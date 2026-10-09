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
  get_multiple_prices(tickers)    → dict[str, float]
"""

import asyncio
import logging

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


async def get_multiple_prices(tickers: list[str]) -> dict[str, float]:
    """
    Consulta los precios de múltiples activos en paralelo.

    Cumple con AGENTS.md §5.1 (Fase B de ingesta):
      - Un único AsyncClient compartido reutiliza el pool de conexiones HTTP.
      - asyncio.gather lanza todas las corrutinas simultáneamente (O(1) en
        tiempo de espera, independiente del número de tickers).
      - Los tickers sin precio o con error se omiten del resultado.

    Parámetros:
      tickers – Lista de símbolos a consultar (puede estar vacía).

    Retorna:
      Diccionario {ticker: precio} con solo los tickers que devolvieron
      un precio válido. Los fallidos no aparecen (no None).
    """
    if not tickers:
        return {}

    async with httpx.AsyncClient() as client:
        # Lanza todas las peticiones en paralelo.
        # asyncio.gather devuelve los resultados en el mismo orden que las corrutinas.
        results: list[float | None] = await asyncio.gather(
            *[get_ticker_price(ticker, client=client) for ticker in tickers]
        )

    # Filtra los tickers que devolvieron None (error o sin datos)
    prices: dict[str, float] = {
        ticker: price
        for ticker, price in zip(tickers, results)
        if price is not None
    }

    if len(prices) < len(tickers):
        failed = [t for t in tickers if t not in prices]
        logger.warning("Sin precio para los siguientes tickers: %s", failed)

    return prices


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
        print("── Prueba de consulta múltiple (paralela) ─────────────────")
        tickers = ["GGAL.BA", "YPFD.BA", "AL30.BA", "AAPL.BA", "TICKER_INVALIDO"]
        precios = await get_multiple_prices(tickers)
        for t in tickers:
            if t in precios:
                print(f"  {t:<20} → ${precios[t]:,.4f} ARS")
            else:
                print(f"  {t:<20} → sin datos")

    _asyncio.run(_demo())
