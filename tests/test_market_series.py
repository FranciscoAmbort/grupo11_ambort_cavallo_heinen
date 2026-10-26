"""Tests de app/services/market_series.py: parseo de la serie de Yahoo y armado multi-activo (sin red)."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from app.services.market_series import Candle, PriceSeries, get_multiple_series, parse_chart_series

# 23/10/2026 14:00, 14:01, 14:02, 14:03 UTC (11:00 a 11:03 en Argentina)
T0 = 1792764000
TIMESTAMPS = [T0, T0 + 60, T0 + 120, T0 + 180]


def _utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _chart(timestamps=TIMESTAMPS, quote=None, meta=None) -> dict:
    """Respuesta mínima de /v8/finance/chart con la forma real de Yahoo."""
    if quote is None:
        quote = {
            "open": [5950.0, 5960.0, None, 5940.0],
            "close": [5960.0, 5945.5, None, 5952.25],
            "high": [5965.0, 5961.0, None, 5955.0],
            "low": [5930.0, 5940.0, None, 5938.0],
            "volume": [9271, 1200, None, 800],
        }
    result = {
        "meta": {"currency": "ARS", "dataGranularity": "1m", "regularMarketTime": T0 + 215}
        if meta is None
        else meta,
        "timestamp": timestamps,
        "indicators": {"quote": [quote]},
    }
    return {"chart": {"result": [result], "error": None}}


# ── parse_chart_series ────────────────────────────────────────────────────────

def test_parse_builds_candles_in_order_and_skips_null_minutes():
    series = parse_chart_series(_chart())

    assert [c.timestamp for c in series.candles] == [_utc(T0), _utc(T0 + 60), _utc(T0 + 180)]
    first = series.candles[0]
    assert first == Candle(_utc(T0), Decimal("5960.0000"), Decimal("5965.0000"), Decimal("5930.0000"))
    assert series.candles[1].close == Decimal("5945.5000")
    assert series.as_of == _utc(T0 + 215)
    assert series.snapshot is False


def test_parse_returns_decimals_with_four_places():
    data = _chart(timestamps=[T0], quote={"close": [61.759998], "high": [61.8], "low": [61.7]})
    candle = parse_chart_series(data).candles[0]
    assert isinstance(candle.close, Decimal)
    assert candle.close == Decimal("61.7600")
    assert candle.close.as_tuple().exponent == -4


def test_parse_uses_close_when_high_or_low_missing():
    # Sin listas high/low
    no_lists = parse_chart_series(_chart(timestamps=[T0], quote={"close": [100.5]}))
    assert no_lists.candles == [Candle(_utc(T0), Decimal("100.5000"), Decimal("100.5000"), Decimal("100.5000"))]

    # high/low en null solo en una vela, o listas más cortas que timestamp
    data = _chart(timestamps=[T0, T0 + 60], quote={"close": [10.0, 11.0], "high": [None], "low": [9.5, None]})
    first, second = parse_chart_series(data).candles
    assert (first.high, first.low) == (Decimal("10.0000"), Decimal("9.5000"))
    assert (second.high, second.low) == (Decimal("11.0000"), Decimal("11.0000"))


def test_parse_fixes_inconsistent_high_low():
    data = _chart(timestamps=[T0], quote={"close": [100.0], "high": [99.0], "low": [101.0]})
    candle = parse_chart_series(data).candles[0]
    assert candle.high == Decimal("100.0000")
    assert candle.low == Decimal("100.0000")


def test_parse_skips_invalid_numbers_and_timestamps():
    data = _chart(
        timestamps=[T0, None, T0 + 120, "x", T0 + 240],
        quote={"close": [float("nan"), 5.0, True, 7.0, "8"], "high": [], "low": []},
    )
    assert parse_chart_series(data).candles == []


def test_parse_without_data():
    # Rueda sin operaciones todavía: Yahoo devuelve meta pero no timestamp ni precios
    data = {"chart": {"result": [{"meta": {"regularMarketTime": T0}, "indicators": {"quote": [{}]}}]}}
    series = parse_chart_series(data)
    assert series.candles == []
    assert series.as_of == _utc(T0)

    only_nulls = _chart(timestamps=[T0, T0 + 60], quote={"close": [None, None], "high": [None, None], "low": [None, None]})
    assert parse_chart_series(only_nulls).candles == []


def test_parse_falls_back_to_last_timestamp_for_as_of():
    series = parse_chart_series(_chart(meta={"currency": "ARS"}))
    assert series.as_of == _utc(T0 + 180)


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        "texto",
        {},
        {"chart": None},
        {"chart": {"result": None}},
        {"chart": {"result": []}},
        {"chart": {"result": [{"timestamp": [T0]}]}},
        {"chart": {"result": [{"timestamp": [T0], "indicators": {"quote": []}}]}},
        {"chart": {"result": [{"timestamp": [T0], "indicators": {"quote": [None]}}]}},
        {"chart": {"error": {"code": "Not Found", "description": "No data found, symbol may be delisted"}}},
    ],
    ids=["none", "lista", "texto", "vacio", "chart-null", "result-null", "result-vacio",
         "sin-indicators", "quote-vacio", "quote-null", "error-yahoo"],
)
def test_parse_broken_structure_returns_empty_series(data):
    series = parse_chart_series(data)
    assert series == PriceSeries([], None)


# ── get_multiple_series ───────────────────────────────────────────────────────

BONDS_DATA = {
    "rueda": {"abierta": True},
    "bonds": [{"ticker": "AL30D", "tickerBase": "AL30", "precio": 61.75}],
}


def _run_multiple(handler, tickers, **kwargs):
    async def call():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await get_multiple_series(tickers, client=client, **kwargs)

    return asyncio.run(call())


def test_multiple_series_yahoo_and_bonds():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "FALLA.BA" in request.url.path:
            return httpx.Response(500, text="error")
        return httpx.Response(200, json=_chart())

    now = datetime(2026, 10, 23, 14, 5, 30, tzinfo=timezone.utc)
    result = _run_multiple(
        handler,
        {"GGAL.BA": "stock", "AAPL.BA": "cedear", "FALLA.BA": "stock", "AL30": "bond"},
        bonds_data=BONDS_DATA,
        interval="5m",
        now=now,
    )

    assert set(result) == {"GGAL.BA", "AAPL.BA", "AL30"}
    assert len(result["GGAL.BA"].candles) == 3
    assert result["AL30"] == PriceSeries([Candle(now, Decimal("61.7500"), Decimal("61.7500"), Decimal("61.7500"))], now, True)

    # Una sola consulta por ticker de Yahoo (los bonos no van a Yahoo), con interval, range y User-Agent
    assert sorted(r.url.path.rsplit("/", 1)[-1] for r in requests) == ["AAPL.BA", "FALLA.BA", "GGAL.BA"]
    for request in requests:
        assert request.url.params["interval"] == "5m"
        assert request.url.params["range"] == "1d"
        assert request.headers["User-Agent"].startswith("Mozilla/5.0")


def test_multiple_series_skips_tickers_without_candles():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"chart": {"result": [{"meta": {}, "indicators": {"quote": [{}]}}]}})

    assert _run_multiple(handler, {"GGAL.BA": "stock"}) == {}


def test_multiple_series_limits_concurrency():
    active = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return httpx.Response(200, json=_chart())

    tickers = {f"T{i}.BA": "stock" for i in range(12)}
    result = _run_multiple(handler, tickers)

    assert len(result) == 12
    assert peak == 5
