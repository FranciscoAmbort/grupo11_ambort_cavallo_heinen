"""Tests de app/worker/evaluation.py (funciones puras) y del texto de las notificaciones del worker."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.services.market_series import Candle, PriceSeries
from app.worker.evaluation import (
    AlertResult,
    AssetCursor,
    candles_to_store,
    complete_candles,
    evaluate_alert,
    select_candles_to_evaluate,
)

MINUTE = timedelta(minutes=1)
# 26/10/2026 14:00 UTC = 11:00 en Argentina
T0 = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)
LONG_AGO = T0 - timedelta(days=1)


def candle(minute: int, close, high=None, low=None) -> Candle:
    """Vela que empieza `minute` minutos después de T0 (precios como texto o número)."""
    close = Decimal(str(close))
    return Candle(
        T0 + minute * MINUTE,
        close,
        Decimal(str(high)) if high is not None else close,
        Decimal(str(low)) if low is not None else close,
    )


def at(minute: int, seconds: int = 0) -> datetime:
    return T0 + minute * MINUTE + timedelta(seconds=seconds)


# ── evaluate_alert: umbral fijo ───────────────────────────────────────────────

def test_price_min_triggers_on_low_even_if_close_does_not_cross():
    candles = [candle(0, 100, 101, 99.5), candle(1, 100, 100.5, 94.0), candle(2, 101, 101, 100)]
    result = evaluate_alert("price_min", Decimal("95"), None, LONG_AGO, candles)
    assert result == AlertResult(candles[1], Decimal("94.0"), True)


def test_price_max_triggers_on_high_even_if_close_does_not_cross():
    candles = [candle(0, 100, 101, 99), candle(1, 100, 106.5, 99.8)]
    result = evaluate_alert("price_max", Decimal("105"), None, LONG_AGO, candles)
    assert result == AlertResult(candles[1], Decimal("106.5"), True)


def test_cross_in_the_middle_of_the_series_that_later_recovers():
    # Cae por debajo del piso a mitad de la serie y después vuelve: el disparo igual se detecta
    candles = [candle(0, 100), candle(1, 97, 98, 96), candle(2, 93, 96, 92.5), candle(3, 99), candle(4, 101)]
    result = evaluate_alert("price_min", Decimal("95"), None, LONG_AGO, candles)
    assert result.candle == candles[2]
    assert result.price == Decimal("92.5")


def test_no_cross_returns_none():
    candles = [candle(0, 100, 104, 96), candle(1, 101, 104.99, 95.01)]
    assert evaluate_alert("price_max", Decimal("105"), None, LONG_AGO, candles) is None
    assert evaluate_alert("price_min", Decimal("95"), None, LONG_AGO, candles) is None
    assert evaluate_alert("percentage", Decimal("5"), Decimal("100"), LONG_AGO, candles) is None


def test_only_one_trigger_even_if_several_candles_cross():
    candles = [candle(0, 100), candle(1, 110, 112, 100), candle(2, 111, 115, 110), candle(3, 120, 125, 119)]
    result = evaluate_alert("price_max", Decimal("105"), None, LONG_AGO, candles)
    assert result.candle == candles[1]
    assert result.price == Decimal("112")


def test_threshold_equal_to_high_or_low_triggers():
    candles = [candle(0, 100, 105, 95)]
    assert evaluate_alert("price_max", Decimal("105"), None, LONG_AGO, candles).triggered
    assert evaluate_alert("price_min", Decimal("95"), None, LONG_AGO, candles).triggered


# ── evaluate_alert: variación porcentual ──────────────────────────────────────

def test_percentage_rise_by_high():
    candles = [candle(0, 100.5, 101, 100), candle(1, 101, 103.2, 100.4)]
    result = evaluate_alert("percentage", Decimal("3"), Decimal("100"), LONG_AGO, candles)
    assert result.candle == candles[1]
    assert result.price == Decimal("103.2")
    assert result.change_pct == Decimal("3.2")


def test_percentage_fall_by_low():
    candles = [candle(0, 99, 100, 98), candle(1, 99.5, 100, 96.5)]
    result = evaluate_alert("percentage", Decimal("3"), Decimal("100"), LONG_AGO, candles)
    assert result.candle == candles[1]
    assert result.price == Decimal("96.5")
    assert result.change_pct == Decimal("-3.5")


def test_percentage_rise_and_fall_in_same_candle_uses_the_larger():
    # Suba 4 % y baja 6 % en la misma vela: gana la baja
    falls_more = [candle(0, 100, 104, 94)]
    result = evaluate_alert("percentage", Decimal("3"), Decimal("100"), LONG_AGO, falls_more)
    assert (result.price, result.change_pct) == (Decimal("94"), Decimal("-6"))

    # Suba 7 % y baja 5 %: gana la suba
    rises_more = [candle(0, 100, 107, 95)]
    result = evaluate_alert("percentage", Decimal("3"), Decimal("100"), LONG_AGO, rises_more)
    assert (result.price, result.change_pct) == (Decimal("107"), Decimal("7"))

    # Empate: suba
    tie = [candle(0, 100, 105, 95)]
    result = evaluate_alert("percentage", Decimal("3"), Decimal("100"), LONG_AGO, tie)
    assert result.price == Decimal("105")


def test_percentage_reanchor_measures_next_cycle_from_reached_price():
    # Ciclo 1: dispara al llegar a 103 y la nueva referencia es 103
    first = evaluate_alert("percentage", Decimal("3"), Decimal("100"), LONG_AGO, [candle(0, 102, 103, 101)])
    assert first.triggered and first.price == Decimal("103")

    # Ciclo 2: 105 es +5 % contra 100, pero solo +1,94 % contra la nueva referencia: no dispara
    assert evaluate_alert("percentage", Decimal("3"), first.price, LONG_AGO, [candle(1, 104, 105, 103)]) is None

    # Ciclo 3: 106,2 sí llega al 3 % contra 103
    third = evaluate_alert("percentage", Decimal("3"), first.price, LONG_AGO, [candle(2, 106, 106.2, 104)])
    assert third.triggered and third.price == Decimal("106.2")


def test_percentage_without_reference_anchors_to_last_close_without_triggering():
    candles = [candle(0, 100, 150, 50), candle(1, 101, 102, 100)]
    result = evaluate_alert("percentage", Decimal("1"), None, LONG_AGO, candles)
    assert result == AlertResult(candles[1], Decimal("101"), False)


def test_percentage_with_non_positive_reference_does_nothing():
    assert evaluate_alert("percentage", Decimal("1"), Decimal("0"), LONG_AGO, [candle(0, 100)]) is None


def test_unknown_alert_type_does_nothing():
    assert evaluate_alert("otro", Decimal("1"), None, LONG_AGO, [candle(0, 100)]) is None


# ── evaluate_alert: creación de la alerta ─────────────────────────────────────

def test_candles_before_alert_creation_are_ignored():
    candles = [candle(0, 90, 91, 89), candle(1, 100, 101, 99), candle(2, 100, 100, 94)]

    # La alerta se creó a las 14:01:30: la vela de 14:00 (que cruzaba) y la de 14:01
    # (que empezó antes de la creación) no cuentan; dispara recién en 14:02
    result = evaluate_alert("price_min", Decimal("95"), None, at(1, 30), candles)
    assert result.candle == candles[2]

    # Si todas las velas son anteriores, no hay nada que evaluar
    assert evaluate_alert("price_min", Decimal("95"), None, at(3), candles) is None


def test_percentage_anchor_ignores_candles_before_creation():
    candles = [candle(0, 90), candle(1, 100)]
    result = evaluate_alert("percentage", Decimal("1"), None, at(1), candles)
    assert result.price == Decimal("100")
    assert evaluate_alert("percentage", Decimal("1"), None, at(2), candles) is None


# ── complete_candles ──────────────────────────────────────────────────────────

def _series(*candles, as_of=None, snapshot=False) -> PriceSeries:
    return PriceSeries(list(candles), as_of, snapshot)


def test_candle_in_progress_is_excluded():
    candles = [candle(0, 1), candle(1, 2), candle(2, 3)]
    # El último dato es de 14:02:40: la vela de 14:02 está en curso
    complete = complete_candles(_series(*candles, as_of=at(2, 40)), MINUTE, now=at(2, 45))
    assert complete == candles[:2]


def test_candle_is_complete_once_its_interval_ended():
    candles = [candle(0, 1), candle(1, 2)]
    complete = complete_candles(_series(*candles, as_of=at(2, 0)), MINUTE, now=at(2, 5))
    assert complete == candles


def test_delayed_data_uses_last_data_time_not_now():
    # Datos con 20 minutos de demora: la última vela (14:02) sigue en curso aunque "ahora" sea 14:22
    candles = [candle(0, 1), candle(1, 2), candle(2, 3)]
    complete = complete_candles(_series(*candles, as_of=at(2, 30)), MINUTE, now=at(22))
    assert complete == candles[:2]


def test_last_candle_without_trades_is_complete_after_its_minute():
    # Activo poco líquido: último dato a las 14:01:10 pero ya son 14:05 → la vela 14:01 sigue "en curso"
    # para Yahoo (as_of dentro de ella); recién cuando haya un dato posterior queda completa
    candles = [candle(0, 1), candle(1, 2)]
    assert complete_candles(_series(*candles, as_of=at(1, 10)), MINUTE, now=at(5)) == candles[:1]
    assert complete_candles(_series(*candles, as_of=at(4, 10)), MINUTE, now=at(5)) == candles


def test_bond_snapshot_is_complete():
    snapshot = _series(candle(3, 61.5), as_of=at(3), snapshot=True)
    assert complete_candles(snapshot, MINUTE, now=at(3)) == snapshot.candles


def test_series_without_as_of_uses_now():
    candles = [candle(0, 1), candle(1, 2)]
    assert complete_candles(_series(*candles), MINUTE, now=at(1, 30)) == candles[:1]


# ── select_candles_to_evaluate ────────────────────────────────────────────────

def test_startup_without_state_evaluates_only_last_complete_candle():
    complete = [candle(0, 1), candle(1, 2), candle(2, 3)]
    pending, cursor = select_candles_to_evaluate(complete, AssetCursor(floor=None))
    assert pending == [complete[-1]]
    assert cursor.evaluated_until == complete[-1].timestamp


def test_each_complete_candle_is_evaluated_only_once():
    cursor = AssetCursor(floor=None)
    seen: list[Candle] = []

    # Ciclo 1 (arranque): velas completas 14:00-14:02
    pending, cursor = select_candles_to_evaluate([candle(0, 1), candle(1, 2), candle(2, 3)], cursor)
    seen += pending
    # Ciclo 2: misma serie (nada nuevo cerró)
    pending, cursor = select_candles_to_evaluate([candle(0, 1), candle(1, 2), candle(2, 3)], cursor)
    seen += pending
    # Ciclo 3: cerraron 14:03 y 14:04
    pending, cursor = select_candles_to_evaluate([candle(m, m) for m in range(5)], cursor)
    seen += pending
    # Ciclo 4: cerró 14:05
    pending, cursor = select_candles_to_evaluate([candle(m, m) for m in range(6)], cursor)
    seen += pending

    assert [c.timestamp for c in seen] == [at(2), at(3), at(4), at(5)]


def test_never_evaluates_candles_before_floor():
    # Al deploy la última lectura guardada era de 14:03:20 (sistema anterior)
    cursor = AssetCursor(floor=at(3, 20))

    # Arranque: la última vela completa (14:03) es anterior al piso → no se evalúa
    pending, cursor = select_candles_to_evaluate([candle(m, m) for m in range(4)], cursor)
    assert pending == []
    assert cursor.evaluated_until == at(3)

    # Siguiente ciclo: solo las velas desde el piso
    pending, cursor = select_candles_to_evaluate([candle(m, m) for m in range(6)], cursor)
    assert [c.timestamp for c in pending] == [at(4), at(5)]


def test_restart_evaluates_the_candle_that_was_in_progress():
    # Reinicio: la última lectura guardada era la vela en curso de 14:05
    cursor = AssetCursor(floor=at(5), last_saved=at(5))
    pending, cursor = select_candles_to_evaluate([candle(m, m) for m in range(5)], cursor)  # 14:05 sigue en curso
    assert pending == []
    pending, cursor = select_candles_to_evaluate([candle(m, m) for m in range(6)], cursor)  # 14:05 cerró
    assert [c.timestamp for c in pending] == [at(5)]


def test_no_complete_candles_keeps_cursor():
    cursor = AssetCursor(floor=None, evaluated_until=at(3))
    assert select_candles_to_evaluate([], cursor) == ([], cursor)


# ── candles_to_store ──────────────────────────────────────────────────────────

def test_candles_to_store_without_history_stores_whole_series():
    series = _series(candle(0, 1), candle(1, 2))
    assert candles_to_store(series, None) == series.candles


def test_candles_to_store_resends_the_candle_in_progress():
    series = _series(*[candle(m, m) for m in range(5)])
    # La última guardada fue la vela de 14:03, que estaba en curso: se reenvía para actualizarla
    assert [c.timestamp for c in candles_to_store(series, at(3))] == [at(3), at(4)]


# ── Texto de la notificación ──────────────────────────────────────────────────

def test_trigger_message_shows_reached_price_and_candle_time_in_argentina():
    from app.models import Alert, Asset
    from app.worker.checker import _trigger_message

    asset = Asset(ticker="GGAL.BA", name="Grupo Financiero Galicia", currency="ARS")
    hit = candle(31, 6250, 6270, 6240)  # 14:31 UTC = 11:31 en Argentina

    price_max = Alert(alert_type="price_max", threshold_value=Decimal("6265"))
    message = _trigger_message(price_max, asset, AlertResult(hit, hit.high, True))
    assert "Precio alcanzado: <b>$6,270.00 ARS</b> a las 11:31 (hora Argentina)" in message
    assert "Umbral fijado: <b>$6,265.00 ARS</b>" in message

    percentage = Alert(alert_type="percentage", threshold_value=Decimal("2"), last_notified_price=Decimal("6400"))
    message = _trigger_message(percentage, asset, AlertResult(hit, hit.low, True, Decimal("-2.5")))
    assert "Variación: <b>2.50%</b> (bajó) a las 11:31 (hora Argentina)" in message
    assert "Precio alcanzado: <b>$6,240.00 ARS</b>" in message
    assert "Precio referencia anterior: <b>$6,400.00 ARS</b>" in message


@pytest.mark.parametrize("alert_type", ["price_min", "price_max", "percentage"])
def test_trigger_message_for_every_type(alert_type):
    from app.models import Alert, Asset
    from app.worker.checker import _trigger_message

    asset = Asset(ticker="AL30", name="Bono", currency="USD")
    hit = candle(0, 61.5)
    alert = Alert(alert_type=alert_type, threshold_value=Decimal("1"), last_notified_price=Decimal("60"))
    message = _trigger_message(alert, asset, AlertResult(hit, hit.close, True, Decimal("2.5")))
    assert "AL30" in message and "11:00" in message and "USD" in message
