"""
app/services/market.py
──────────────────────
Constantes compartidas para consultar Yahoo Finance y servicio de bonos y estado
del mercado desde la API pública de CompararFondos.

Reglas de arquitectura aplicadas (ver AGENTS.md §5.2):
  - Toda petición HTTP usa httpx.AsyncClient (prohibido requests).
  - Timeout explícito de 10 segundos por petición.
  - Cabecera User-Agent simulando un navegador en cada llamada a Yahoo.

Constantes que reutilizan market_series.py y catalog.py:
  _YF_URL, _HEADERS, _TIMEOUT

Funciones públicas:
  fetch_bonds_data()                           → dict | None   (respuesta completa de /api/bonos)
  is_market_open(bonds_data)                   → bool | None   (según rueda.abierta)
  fetch_bonds_prices(tickers, bonds_data=None) → dict[str, float]
"""

import logging

import httpx

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────────────────

# Endpoint de gráficos v8 de Yahoo Finance (host query1); {ticker} se completa con un
# ticker ya validado. Lo reutilizan market_series.py y catalog.py.
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


# ── Funciones públicas ────────────────────────────────────────────────────────

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
