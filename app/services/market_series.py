"""
app/services/market_series.py
─────────────────────────────
Series intradiarias de precios (velas) para evaluar alertas sobre los valores
intermedios de cada consulta, incluidos el máximo y el mínimo de cada vela.

Módulo aparte de app/services/market.py para no chocar con los cambios en ese
archivo: reutiliza su URL, cabeceras, timeout y la consulta de bonos sin modificarlos.

Reglas aplicadas (ver AGENTS.md §5.2):
  - httpx.AsyncClient con el mismo timeout (10 s) y User-Agent que market.py.
  - Consultas concurrentes con asyncio.gather, limitadas con un asyncio.Semaphore.
  - Precios como Decimal.

Funciones públicas:
  parse_chart_series(data)                     → PriceSeries   (pura, sin red)
  get_ticker_series(ticker, client, semaphore) → PriceSeries | None
  get_multiple_series({ticker: asset_type})    → dict[str, PriceSeries]
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import logging

import httpx

from app.services.market import _HEADERS, _TIMEOUT, _YF_URL, fetch_bonds_prices

logger = logging.getLogger(__name__)

# Solo la rueda en curso (o la última, si todavía no abrió)
_SERIES_RANGE = "1d"
# Máximo de consultas simultáneas a Yahoo
_MAX_CONCURRENT_REQUESTS = 5
# Misma escala que la columna price_history.price (Numeric(12, 4))
_PRICE_QUANTUM = Decimal("0.0001")


@dataclass(frozen=True)
class Candle:
    """Vela de precio: inicio del intervalo (UTC) y precios de cierre, máximo y mínimo."""

    timestamp: datetime
    close: Decimal
    high: Decimal
    low: Decimal


@dataclass(frozen=True)
class PriceSeries:
    """Velas de un activo en orden cronológico y hasta cuándo hay datos."""

    candles: list[Candle]
    # Hora del último dato (meta.regularMarketTime o, si falta, el último timestamp de la respuesta).
    # La vela que contiene este instante todavía está en curso.
    as_of: datetime | None
    # True para los bonos: una sola lectura puntual, completa desde que se toma
    snapshot: bool = False


def _to_price(value) -> Decimal | None:
    """Convierte un número del JSON a Decimal con 4 decimales; None si falta o no es un número finito."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        price = Decimal(str(value))
    except InvalidOperation:
        return None
    if not price.is_finite():
        return None
    return price.quantize(_PRICE_QUANTUM)


def _item(values: list, index: int):
    """Elemento `index` de la lista o None si la lista es más corta."""
    return values[index] if index < len(values) else None


def _utc(epoch) -> datetime | None:
    """Segundos epoch → datetime UTC; None si no es un número."""
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)):
        return None
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc)


def parse_chart_series(data) -> PriceSeries:
    """
    Arma la serie de velas a partir de la respuesta de /v8/finance/chart, sin hacer red.

    Usa timestamp[] e indicators.quote[0].close/high/low. Descarta las velas sin close
    (minutos sin operaciones, que Yahoo manda en null); si falta high o low usa el close.
    Ante una estructura inesperada devuelve una serie vacía.
    """
    try:
        result = data["chart"]["result"][0]
        timestamps = result.get("timestamp") or []
        quote = result["indicators"]["quote"][0]
        meta = result.get("meta") or {}
        closes = quote.get("close") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
    except (KeyError, IndexError, TypeError, AttributeError):
        return PriceSeries([], None)

    by_timestamp: dict[datetime, Candle] = {}
    for index, epoch in enumerate(timestamps):
        moment = _utc(epoch)
        close = _to_price(_item(closes, index))
        if moment is None or close is None:
            continue
        high = _to_price(_item(highs, index))
        low = _to_price(_item(lows, index))
        by_timestamp[moment] = Candle(
            timestamp=moment,
            close=close,
            # Un máximo por debajo del cierre (o un mínimo por encima) sería un dato inconsistente
            high=close if high is None else max(high, close),
            low=close if low is None else min(low, close),
        )

    as_of = _utc(meta.get("regularMarketTime") if isinstance(meta, dict) else None)
    if as_of is None:
        as_of = next((m for m in map(_utc, reversed(timestamps)) if m is not None), None)

    return PriceSeries([by_timestamp[moment] for moment in sorted(by_timestamp)], as_of)


async def get_ticker_series(
    ticker: str,
    *,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    interval: str,
) -> PriceSeries | None:
    """Consulta la serie intradiaria de un activo en Yahoo Finance; None si la consulta falla."""
    url = _YF_URL.format(ticker=ticker)
    params = {"interval": interval, "range": _SERIES_RANGE}
    async with semaphore:
        try:
            response = await client.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException:
            logger.warning("Timeout al consultar la serie de '%s' en Yahoo Finance.", ticker)
            return None
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "HTTP %s al consultar la serie de '%s': %s",
                exc.response.status_code,
                ticker,
                exc.response.text[:200],
            )
            return None
        except httpx.RequestError as exc:
            logger.warning("Error de red al consultar la serie de '%s': %s", ticker, exc)
            return None
        except ValueError:
            logger.warning("Yahoo Finance devolvió una respuesta que no es JSON para '%s'.", ticker)
            return None
    return parse_chart_series(data)


async def get_multiple_series(
    tickers: Mapping[str, str],
    *,
    bonds_data: dict | None = None,
    interval: str = "1m",
    client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
) -> dict[str, PriceSeries]:
    """
    Consulta en paralelo las series de velas de varios activos, una sola vez por ticker.

    Parámetros:
      tickers    – Mapeo {ticker: asset_type}.
      bonds_data – Respuesta de la API de bonos ya descargada (opcional).
      interval   – Ancho de vela de Yahoo (settings.yahoo_interval).
      client     – AsyncClient reutilizable (opcional; permite inyectar un transporte en pruebas).
      now        – Hora de la lectura de los bonos (por defecto, ahora).

    Acciones y CEDEARs reciben la serie de Yahoo; los bonos, una sola vela con
    high = low = close (precio de CompararFondos y la hora actual). Solo se incluyen
    los tickers con al menos una vela.
    """
    stock_tickers = [ticker for ticker, asset_type in tickers.items() if asset_type != "bond"]
    bond_tickers = [ticker for ticker, asset_type in tickers.items() if asset_type == "bond"]
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_REQUESTS)

    async def _fetch_yahoo(c: httpx.AsyncClient) -> dict[str, PriceSeries]:
        results = await asyncio.gather(
            *[get_ticker_series(ticker, client=c, semaphore=semaphore, interval=interval) for ticker in stock_tickers]
        )
        return {ticker: series for ticker, series in zip(stock_tickers, results) if series and series.candles}

    async def _yahoo() -> dict[str, PriceSeries]:
        if not stock_tickers:
            return {}
        if client is not None:
            return await _fetch_yahoo(client)
        async with httpx.AsyncClient(follow_redirects=True) as c:
            return await _fetch_yahoo(c)

    yahoo_series, bond_prices = await asyncio.gather(
        _yahoo(), fetch_bonds_prices(bond_tickers, bonds_data=bonds_data)
    )

    moment = now or datetime.now(timezone.utc)
    bond_series: dict[str, PriceSeries] = {}
    for ticker, raw_price in bond_prices.items():
        price = _to_price(raw_price)
        if price is not None:
            candle = Candle(timestamp=moment, close=price, high=price, low=price)
            bond_series[ticker] = PriceSeries([candle], as_of=moment, snapshot=True)

    unified = {**yahoo_series, **bond_series}
    missing = [ticker for ticker in tickers if ticker not in unified]
    if missing:
        logger.warning("Sin serie de precios para %d de %d tickers: %s", len(missing), len(tickers), missing)
    return unified


# ── Prueba rápida en consola ──────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    from app.config import settings

    async def _demo() -> None:
        """Muestra la serie de los tickers pasados por argumento (por defecto GGAL.BA)."""
        tickers = sys.argv[1:] or ["GGAL.BA"]
        series = await get_multiple_series({ticker: "stock" for ticker in tickers}, interval=settings.yahoo_interval)
        for ticker in tickers:
            data = series.get(ticker)
            if data is None:
                print(f"{ticker}: sin datos")
                continue
            first, last = data.candles[0], data.candles[-1]
            print(f"{ticker}: {len(data.candles)} velas · as_of {data.as_of:%d/%m %H:%M:%S} UTC")
            print(f"  primera {first.timestamp:%H:%M} UTC  close {first.close}  high {first.high}  low {first.low}")
            print(f"  última  {last.timestamp:%H:%M} UTC  close {last.close}  high {last.high}  low {last.low}")

    asyncio.run(_demo())
