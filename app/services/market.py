"""
app/services/market.py
──────────────────────
Servicio de ingesta de cotizaciones desde Yahoo Finance.

Reglas de arquitectura aplicadas (ver AGENTS.md §5.2):
  - Toda petición HTTP usa httpx.AsyncClient (prohibido requests).
  - Timeout explícito de 10 segundos por petición.
  - Cabecera User-Agent simulando un navegador en cada llamada.
  - Las consultas múltiples se paralelizan con asyncio.gather (prohibido
    un for-loop secuencial con await para I/O externo).

Funciones públicas:
  get_ticker_price(ticker)        → float | None
  fetch_bonds_data()              → dict | None   (respuesta completa de /api/bonos)
  is_market_open(bonds_data)      → bool | None   (según rueda.abierta)
  get_multiple_prices(tickers)    → dict[str, float]
"""

import asyncio
from collections import abc as collections_abc
import logging
import typing

import httpx

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────────────────

# URL base del endpoint de Yahoo Finance v8.
# Se usa query2 en lugar de query1: misma API, distinto subdominio de balanceo
# que históricamente recibe menos rate-limiting en IPs no autenticadas.
_YF_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"

# Cabecera User-Agent obligatoria (AGENTS.md §5.2).
# Sin ella Yahoo Finance suele devolver 429 Too Many Requests o respuestas vacías.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Timeout máximo por petición HTTP (AGENTS.md §5.2).
_TIMEOUT = httpx.Timeout(10.0)

# Parámetros mínimos para obtener el precio actual (último cierre o precio
# de mercado intradiario) sin descargar series históricas completas.
_PARAMS = {
    "interval": "1d",
    "range": "1d",
}


# ── Funciones privadas ────────────────────────────────────────────────────────

def _extract_price(data: dict) -> float | None:
    """
    Extrae el precio relevante de la respuesta JSON de Yahoo Finance v8.

    Jerarquía de extracción (de más a menos preferida):
      1. regularMarketPrice  → precio durante sesión de mercado abierta.
      2. chartPreviousClose  → último precio de cierre (fuera de horario).
      3. previousClose       → cierre anterior (fallback adicional).

    Devuelve None si ninguno de los campos está disponible o el JSON
    tiene una estructura inesperada.
    """
    try:
        result = data["chart"]["result"][0]
        meta: dict = result["meta"]

        # Orden de preferencia
        for key in ("regularMarketPrice", "chartPreviousClose", "previousClose"):
            price = meta.get(key)
            if price is not None:
                return float(price)

        logger.warning("Ningún campo de precio encontrado en meta: %s", list(meta.keys()))
        return None

    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.debug("Error al parsear respuesta de Yahoo Finance: %s", exc)
        return None


# ── Funciones públicas ────────────────────────────────────────────────────────

async def get_ticker_price(
    ticker: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> float | None:
    """
    Consulta el precio actual de un único activo en Yahoo Finance.

    Parámetros:
      ticker  – Símbolo exacto del activo (ej. "GGAL.BA", "AL30.BA").
      client  – AsyncClient reutilizable (opcional). Si no se provee,
                se crea uno interno de un solo uso. Proveer un cliente
                compartido es más eficiente cuando se consultan muchos
                tickers (reutiliza conexiones del pool HTTP).

    Retorna:
      El precio como float, o None si la consulta falla o el ticker
      no existe / no tiene datos disponibles.
    """
    url = _YF_URL.format(ticker=ticker)

    async def _fetch(c: httpx.AsyncClient) -> float | None:
        try:
            response = await c.get(url, params=_PARAMS, headers=_HEADERS, timeout=_TIMEOUT)
            response.raise_for_status()
            return _extract_price(response.json())
        except httpx.TimeoutException:
            logger.warning("Timeout al consultar ticker '%s' en Yahoo Finance.", ticker)
            return None
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "HTTP %s al consultar ticker '%s': %s",
                exc.response.status_code,
                ticker,
                exc.response.text[:200],
            )
            return None
        except httpx.RequestError as exc:
            logger.warning("Error de red al consultar ticker '%s': %s", ticker, exc)
            return None

    if client is not None:
        return await _fetch(client)

    # Cliente de un solo uso cuando no se provee uno externo
    async with httpx.AsyncClient(follow_redirects=True) as c:
        return await _fetch(c)


# Endpoint público de CompararFondos para cotizaciones consolidadas de bonos BYMA
_BONDS_API_URL = "https://compararfondos.com.ar/api/bonos"


async def fetch_bonds_data() -> dict | None:
    """
    Hace la petición a la API de bonos de CompararFondos y devuelve su respuesta completa.

    De esa misma respuesta salen los precios de los bonos y el estado del mercado
    (objeto "rueda"), así el worker consulta la API una sola vez por ciclo.
    Retorna None si la API no responde, da error o no devuelve un objeto JSON.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(_BONDS_API_URL, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            data = response.json()
    except httpx.TimeoutException:
        logger.warning("Timeout al consultar la API de bonos compararfondos.com.ar.")
        return None
    except httpx.HTTPStatusError as exc:
        logger.warning("Error HTTP %s al consultar bonos: %s", exc.response.status_code, exc.response.text[:200])
        return None
    except httpx.RequestError as exc:
        logger.warning("Error de red al consultar la API de bonos: %s", exc)
        return None
    except Exception as exc:
        logger.warning("Error inesperado procesando respuesta de bonos: %s", exc)
        return None

    if not isinstance(data, dict):
        logger.warning("Formato inesperado en respuesta de bonos (no es un objeto JSON).")
        return None
    return data


def is_market_open(bonds_data: dict | None) -> bool | None:
    """
    Dice si el mercado está abierto según el campo rueda.abierta de la API de bonos.

    Retorna True o False, o None si no se puede saber (sin respuesta, sin objeto
    "rueda" o sin un valor booleano en "abierta").
    """
    if not isinstance(bonds_data, dict):
        return None
    rueda = bonds_data.get("rueda")
    if not isinstance(rueda, dict):
        return None
    abierta = rueda.get("abierta")
    return abierta if isinstance(abierta, bool) else None


async def fetch_bonds_prices(tickers: list[str], *, bonds_data: dict | None = None) -> dict[str, float]:
    """
    Consulta cotizaciones consolidadas de bonos soberanos en la API de CompararFondos.

    Parámetros:
      tickers    – Lista de tickers solicitados (ej: ["AL30", "GD30", "AE38"]).
      bonds_data – Respuesta de la API ya descargada (opcional). Si se pasa, se
                   reutiliza en vez de volver a consultar la API.

    Retorna:
      Diccionario {ticker: precio} con los bonos encontrados y precio numérico válido.
    """
    if not tickers:
        return {}

    # Normalizar tickers solicitados (ej: "AL30.BA" -> "AL30", "al30" -> "AL30")
    req_map = {t: t.replace(".BA", "").strip().upper() for t in tickers}
    normalized_needed = set(req_map.values())

    data = bonds_data if bonds_data is not None else await fetch_bonds_data()
    if data is None:
        return {}

    bonds_list = data.get("bonds", [])
    if not isinstance(bonds_list, list):
        logger.warning("Formato inesperado en respuesta de bonos (falta lista 'bonds').")
        return {}

    found_by_normalized: dict[str, float] = {}

    for bond in bonds_list:
        if not isinstance(bond, dict):
            continue
        ticker_item = str(bond.get("ticker", "")).strip().upper()
        ticker_base = str(bond.get("tickerBase", "")).strip().upper()
        raw_price = bond.get("precio")

        if raw_price is None:
            continue

        try:
            price_val = float(raw_price)
        except (ValueError, TypeError):
            continue

        for norm in normalized_needed:
            # Coincidencia exacta de ticker base (ej. "AL30") o ticker con sufijo (ej. "AL30D", "AL30")
            if ticker_base == norm or ticker_item == norm or ticker_item.startswith(norm):
                # Si aún no lo tenemos, o si encontramos una variante preferida en MEP ("D")
                if norm not in found_by_normalized:
                    found_by_normalized[norm] = price_val
                elif ticker_item.endswith("D"):  # Preferencia por cotización en moneda MEP
                    found_by_normalized[norm] = price_val

    # Reconstruir diccionario usando el ticker original recibido
    results: dict[str, float] = {}
    for original_t, norm in req_map.items():
        if norm in found_by_normalized:
            results[original_t] = found_by_normalized[norm]

    return results


async def get_multiple_prices(
    tickers: dict[str, str] | typing.Iterable[tuple[str, str] | typing.Sequence[str]],
    *,
    bonds_data: dict | None = None,
) -> dict[str, float]:
    """
    Consulta los precios de múltiples activos en paralelo utilizando Multi-Provider Strategy:
      - Activos bond -> fetch_bonds_prices (CompararFondos API)
      - Activos stock / cedear -> Yahoo Finance API

    Parámetros:
      tickers    – Mapeo {ticker: asset_type} o iterable de pares (ticker, asset_type)
                   (soporta tuplas, listas, objetos Row de SQLAlchemy o cualquier secuencia de 2 elementos).
      bonds_data – Respuesta de la API de bonos ya descargada (opcional), para no
                   consultarla dos veces en el mismo ciclo.

    Retorna:
      Diccionario {ticker: precio} con solo los tickers que devolvieron un precio válido.
    """
    if not tickers:
        return {}

    # Normalizar a diccionario {ticker: asset_type}
    assets_map: dict[str, str] = {}
    original_input_count = 0

    if isinstance(tickers, dict):
        original_input_count = len(tickers)
        for k, v in tickers.items():
            if isinstance(k, str) and isinstance(v, str):
                assets_map[k] = v
            else:
                logger.warning("Elemento inválido en diccionario de tickers descartado: key=%r, value=%r", k, v)
    else:
        # Iterable de elementos (pares o strings)
        for item in tickers:
            original_input_count += 1
            if isinstance(item, str):
                t_upper = item.upper().replace(".BA", "")
                inferred_type = "bond" if t_upper in ("AL30", "GD30", "AE38") else "stock"
                assets_map[item] = inferred_type
                continue

            # Intentar desempaquetar o indexar como par (ticker, asset_type)
            # Soporta tuple, list, SQLAlchemy Row o cualquier secuencia/iterable de 2 elementos
            is_valid_pair = False
            try:
                if len(item) == 2:  # type: ignore
                    t, a_type = item[0], item[1]  # type: ignore
                    if isinstance(t, str) and isinstance(a_type, str):
                        assets_map[t] = a_type
                        is_valid_pair = True
                    else:
                        logger.warning("Elemento descartado en tickers (los valores del par deben ser strings): %r", item)
                        continue
            except (TypeError, IndexError, KeyError):
                pass

            if not is_valid_pair:
                logger.warning(
                    "Elemento descartado en tickers (se esperaba par (ticker, asset_type) o string): %r (tipo %s)",
                    item,
                    type(item).__name__,
                )

    if not assets_map:
        if original_input_count > 0:
            logger.warning(
                "Se recibieron %d elementos en get_multiple_prices pero ninguno cumplió con el formato esperado.",
                original_input_count,
            )
        return {}

    stocks_tickers: list[str] = [t for t, t_type in assets_map.items() if t_type != "bond"]
    bonds_tickers: list[str] = [t for t, t_type in assets_map.items() if t_type == "bond"]

    async def _fetch_yahoo() -> dict[str, float]:
        if not stocks_tickers:
            return {}
        async with httpx.AsyncClient(follow_redirects=True) as client:
            results = await asyncio.gather(
                *[get_ticker_price(ticker, client=client) for ticker in stocks_tickers]
            )
        return {t: p for t, p in zip(stocks_tickers, results) if p is not None}

    # Despachar ambas llamadas en paralelo con asyncio.gather
    yahoo_future = _fetch_yahoo()
    bonds_future = fetch_bonds_prices(bonds_tickers, bonds_data=bonds_data)

    yahoo_res, bonds_res = await asyncio.gather(yahoo_future, bonds_future)

    unified: dict[str, float] = {**yahoo_res, **bonds_res}

    # Evaluar tickers faltantes contra los tickers válidos normalizados
    if len(unified) < len(assets_map):
        missing = [t for t in assets_map if t not in unified]
        logger.warning(
            "Sin precio para %d de %d tickers solicitados: %s",
            len(missing),
            len(assets_map),
            missing,
        )

    return unified


# ── Prueba rápida en consola ──────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio as _asyncio

    async def _demo() -> None:
        print("── Prueba de consulta individual ─────────────────────────")
        precio = await get_ticker_price("GGAL.BA")
        if precio is not None:
            print(f"  GGAL.BA → ${precio:,.4f} ARS")
        else:
            print("  GGAL.BA → sin datos (mercado cerrado o ticker inválido)")

        print()
        print("── Prueba de consulta múltiple (multi-proveedor) ─────────")
        items = [
            ("GGAL.BA", "stock"),
            ("YPFD.BA", "stock"),
            ("AAPL.BA", "cedear"),
            ("AL30", "bond"),
            ("GD30", "bond"),
            ("AE38", "bond"),
            ("TICKER_INVALIDO", "stock"),
        ]
        precios = await get_multiple_prices(items)
        for t, atype in items:
            if t in precios:
                print(f"  {t:<18} ({atype:<6}) → ${precios[t]:,.4f}")
            else:
                print(f"  {t:<18} ({atype:<6}) → sin datos")

    _asyncio.run(_demo())
