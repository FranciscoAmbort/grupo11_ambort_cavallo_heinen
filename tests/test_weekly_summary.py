"""Tests de las funciones puras de app/services/weekly_summary.py (sin base de datos ni red)."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.services.weekly_summary import (
    EVENTS_DETAIL_LIMIT,
    AlertEventRow,
    AssetWeekRow,
    build_prompt,
    build_stats,
    has_activity,
    is_cache_valid,
    is_summary_current,
    week_range,
)

# Miércoles 21/10/2026 15:00 en Argentina (UTC-3)
WEDNESDAY = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc)


def _asset(ticker, first, last, low, high, readings, *, name=None, asset_type="stock", currency="ARS"):
    """Fila de precios de un activo; los precios se pasan como texto para construir Decimal exactos."""
    to_dec = lambda value: Decimal(value) if value is not None else None  # noqa: E731
    return AssetWeekRow(
        ticker, name or f"Empresa {ticker}", asset_type, currency,
        to_dec(first), to_dec(last), to_dec(low), to_dec(high), readings,
    )


def _no_data(ticker, **kwargs):
    return _asset(ticker, None, None, None, None, 0, **kwargs)


def _event(ticker, alert_type, threshold, price, change_pct, triggered_at):
    return AlertEventRow(
        ticker, alert_type, Decimal(threshold), Decimal(price),
        Decimal(change_pct) if change_pct is not None else None, triggered_at,
    )


@pytest.fixture
def week():
    return week_range(1, WEDNESDAY)


@pytest.fixture
def sample_stats(week):
    assets = [
        _asset("GGAL.BA", "5930", "6120.5", "5880", "6200", 1820, name="Grupo Financiero Galicia"),
        _asset("AL30", "62.45", "61.76", "61.5", "62.8", 1815, asset_type="bond", currency="USD"),
        _no_data("AAPL.BA", name="CEDEAR Apple Inc.", asset_type="cedear"),
    ]
    events = [
        _event("GGAL.BA", "price_max", "6150", "6180", None, datetime(2026, 10, 16, 19, 40, tzinfo=timezone.utc)),
        _event("GGAL.BA", "percentage", "2", "6100", "2.3456", datetime(2026, 10, 14, 17, 31, tzinfo=timezone.utc)),
    ]
    return build_stats(week, assets, {"percentage": 1, "price_max": 1}, events)


# ── week_range ────────────────────────────────────────────────────────────────

def test_week_range_current_week_starts_on_monday():
    week = week_range(0, WEDNESDAY)
    assert week.start == date(2026, 10, 19)
    assert week.end == date(2026, 10, 23)
    assert week.partial is True


def test_week_range_converts_argentina_midnight_to_utc():
    week = week_range(0, WEDNESDAY)
    assert week.start_utc == datetime(2026, 10, 19, 3, 0, tzinfo=timezone.utc)
    assert week.end_utc == datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)


def test_week_range_last_week():
    week = week_range(1, WEDNESDAY)
    assert week.start == date(2026, 10, 12)
    assert week.end == date(2026, 10, 16)
    assert week.partial is False


def test_week_range_uses_argentina_date_not_utc_date():
    # Domingo 25/10 23:30 en Argentina, que en UTC ya es lunes 26/10
    sunday_night = datetime(2026, 10, 26, 2, 30, tzinfo=timezone.utc)
    assert week_range(0, sunday_night).start == date(2026, 10, 19)
    # Lunes 26/10 00:30 en Argentina: ya es la semana siguiente
    monday_early = datetime(2026, 10, 26, 3, 30, tzinfo=timezone.utc)
    assert week_range(0, monday_early).start == date(2026, 10, 26)


@pytest.mark.parametrize("offset", [-1, 2, 5])
def test_week_range_rejects_other_offsets(offset):
    with pytest.raises(ValueError):
        week_range(offset, WEDNESDAY)


# ── build_stats ───────────────────────────────────────────────────────────────

def test_build_stats_positive_and_negative_variation(sample_stats):
    by_ticker = {asset["ticker"]: asset for asset in sample_stats["assets"]}
    ggal = by_ticker["GGAL.BA"]
    assert ggal["has_data"] is True
    assert ggal["first_price"] == 5930.0
    assert ggal["last_price"] == 6120.5
    assert ggal["min_price"] == 5880.0
    assert ggal["max_price"] == 6200.0
    assert ggal["readings"] == 1820
    # (6120.5 - 5930) / 5930 * 100 = 3.2125...
    assert ggal["change_pct"] == 3.21
    # (61.76 - 62.45) / 62.45 * 100 = -1.1048...
    assert by_ticker["AL30"]["change_pct"] == -1.10


def test_build_stats_asset_without_data(sample_stats):
    apple = next(asset for asset in sample_stats["assets"] if asset["ticker"] == "AAPL.BA")
    assert apple["has_data"] is False
    assert apple["readings"] == 0
    for field in ("first_price", "last_price", "min_price", "max_price", "change_pct"):
        assert apple[field] is None
    assert sample_stats["totals"]["assets_count"] == 3
    assert sample_stats["totals"]["assets_with_data"] == 2


def test_build_stats_best_and_worst(sample_stats):
    assert sample_stats["totals"]["best"] == {"ticker": "GGAL.BA", "change_pct": 3.21}
    assert sample_stats["totals"]["worst"] == {"ticker": "AL30", "change_pct": -1.10}


def test_build_stats_single_asset_has_no_separate_worst(week):
    stats = build_stats(week, [_asset("GGAL.BA", "100", "90", "88", "101", 10)], {}, [])
    assert stats["totals"]["best"] == {"ticker": "GGAL.BA", "change_pct": -10.0}
    assert stats["totals"]["worst"] is None


def test_build_stats_first_price_zero_has_no_variation(week):
    stats = build_stats(week, [_asset("XXX.BA", "0", "5", "0", "5", 3)], {}, [])
    assert stats["assets"][0]["has_data"] is True
    assert stats["assets"][0]["change_pct"] is None
    assert stats["totals"]["best"] is None


def test_build_stats_alerts_counts_and_events(sample_stats):
    alerts = sample_stats["alerts"]
    assert alerts["total"] == 2
    assert alerts["by_type"] == {"percentage": 1, "price_max": 1, "price_min": 0}
    assert alerts["events_truncated"] is False
    # Ordenados de más viejo a más nuevo y en hora argentina
    first, second = alerts["events"]
    assert first["triggered_at"] == "14/10/2026 14:31"
    assert first["alert_type"] == "percentage"
    assert first["change_pct"] == 2.35
    assert second["triggered_at"] == "16/10/2026 16:40"
    assert second["change_pct"] is None


def test_build_stats_marks_truncated_events(week):
    base = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)
    events = [
        _event("GGAL.BA", "percentage", "1", "100", "1.5", base + timedelta(minutes=i))
        for i in range(EVENTS_DETAIL_LIMIT)
    ]
    stats = build_stats(week, [], {"percentage": EVENTS_DETAIL_LIMIT + 7}, events)
    assert stats["alerts"]["total"] == EVENTS_DETAIL_LIMIT + 7
    assert len(stats["alerts"]["events"]) == EVENTS_DETAIL_LIMIT
    assert stats["alerts"]["events_truncated"] is True
    assert f"de un total de {EVENTS_DETAIL_LIMIT + 7}" in build_prompt(stats)


def test_build_stats_has_only_market_data_keys(sample_stats):
    assert set(sample_stats) == {"week", "assets", "alerts", "totals"}
    for key in ("user_id", "email", "name", "user"):
        assert key not in sample_stats


# ── has_activity ──────────────────────────────────────────────────────────────

def test_has_activity(week, sample_stats):
    assert has_activity(sample_stats) is True
    assert has_activity(build_stats(week, [_no_data("GGAL.BA")], {}, [])) is False
    only_alert = build_stats(
        week, [_no_data("GGAL.BA")], {"price_min": 1},
        [_event("GGAL.BA", "price_min", "100", "99", None, datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc))],
    )
    assert has_activity(only_alert) is True


# ── build_prompt ──────────────────────────────────────────────────────────────

def test_build_prompt_includes_market_data(sample_stats):
    prompt = build_prompt(sample_stats)
    assert "Semana del lunes 12/10/2026 al viernes 16/10/2026 (semana cerrada)." in prompt
    assert "- GGAL.BA | Grupo Financiero Galicia | acción | ARS | primer precio 5930.00" in prompt
    assert "variación semanal +3.21% | 1820 lecturas" in prompt
    assert "variación semanal -1.10%" in prompt
    assert "- AAPL.BA | CEDEAR Apple Inc. | CEDEAR | ARS | sin datos en la semana" in prompt
    assert "- Mejor: GGAL.BA (+3.21%)" in prompt
    assert "- Peor: AL30 (-1.10%)" in prompt
    assert "ALERTAS DISPARADAS (2)" in prompt
    assert "umbral 2.00% | precio 6100.00 | variación +2.35%" in prompt
    assert "precio máximo (take profit) | umbral 6150.00 | precio 6180.00" in prompt


def test_build_prompt_current_week_and_no_alerts(week):
    current = week_range(0, WEDNESDAY)
    prompt = build_prompt(build_stats(current, [_asset("GGAL.BA", "1", "1", "1", "1", 1)], {}, []))
    assert "(semana en curso, datos hasta ahora)" in prompt
    assert "1 lectura" in prompt and "1 lecturas" not in prompt
    assert "No se disparó ninguna alerta." in prompt


def test_build_prompt_excludes_user_data(sample_stats):
    # Un usuario de prueba: ninguno de sus datos tiene que poder llegar al prompt
    user_email = "maria.gonzalez@example.com"
    user_name = "María González"
    prompt = build_prompt(sample_stats)
    assert user_email not in prompt
    assert user_name not in prompt
    assert "@" not in prompt
    assert "user_id" not in prompt


# ── is_cache_valid ────────────────────────────────────────────────────────────

def test_cache_current_week_lasts_one_hour():
    week = week_range(0, WEDNESDAY)
    assert is_cache_valid(0, WEDNESDAY - timedelta(minutes=59), week, WEDNESDAY) is True
    assert is_cache_valid(0, WEDNESDAY - timedelta(minutes=61), week, WEDNESDAY) is False


def test_cache_last_week_only_if_generated_after_week_closed():
    week = week_range(1, WEDNESDAY)
    # Generado el lunes siguiente, con la semana ya cerrada: vale para siempre
    assert is_cache_valid(1, week.end_utc + timedelta(minutes=5), week, WEDNESDAY) is True
    # Generado a mitad de esa semana (como "Esta semana"): datos parciales, hay que regenerar
    assert is_cache_valid(1, week.end_utc - timedelta(days=3), week, WEDNESDAY) is False


def test_saved_current_week_is_shown_even_if_older_than_one_hour():
    week = week_range(0, WEDNESDAY)
    created_at = WEDNESDAY - timedelta(hours=5)
    # El GET lo muestra aunque el POST ya lo pueda regenerar
    assert is_summary_current(0, created_at, week) is True
    assert is_cache_valid(0, created_at, week, WEDNESDAY) is False


def test_saved_last_week_is_shown_only_if_generated_after_week_closed():
    week = week_range(1, WEDNESDAY)
    assert is_summary_current(1, week.end_utc + timedelta(hours=1), week) is True
    assert is_summary_current(1, week.end_utc - timedelta(days=2), week) is False
