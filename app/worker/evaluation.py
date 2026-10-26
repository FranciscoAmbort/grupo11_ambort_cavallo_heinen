"""
app/worker/evaluation.py
────────────────────────
Funciones puras (sin base de datos ni red) que usa el worker para trabajar con
las velas intradiarias de cada activo:

  candles_to_store(series, last_saved)           → velas a guardar con upsert
  complete_candles(series, interval, now, session_closed=False)
                                                 → velas cerradas (excluye la vela en curso)
  select_candles_to_evaluate(complete, cursor)   → velas todavía no evaluadas y cursor nuevo
  evaluate_alert(type, umbral, ref, creada, velas) → disparo, anclaje o nada
  series_signature(series_map)                   → {ticker: (as_of, última vela)} de Yahoo
  grace_decision(window, signature, now)         → seguir, cerrar por estabilidad o por tiempo

Reglas de evaluación (sobre velas en orden cronológico, máximo un disparo por llamada):
  - price_min:  primera vela con low  <= umbral; precio del disparo = low.
  - price_max:  primera vela con high >= umbral; precio del disparo = high.
  - percentage: variación de suba (high - ref) / ref y de baja (ref - low) / ref;
                dispara en la primera vela donde alguna llegue al umbral (si llegan
                las dos, la mayor) y se re-ancla al precio alcanzado. Sin referencia,
                se ancla al close de la última vela sin disparar.
Las velas que empiezan antes de la creación de la alerta se ignoran.

Ventana de gracia post-cierre (mitigación R1): Yahoo entrega las velas de BYMA con
~20 minutos de demora y, al cerrar la rueda, deja congelado meta.regularMarketTime.
Después del cierre el worker sigue consultando Yahoo hasta que dos pasadas seguidas
devuelven la misma firma (serie estable) o vence el plazo; en esa última pasada las
velas pendientes se cierran a la fuerza (session_closed=True).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from app.services.market_series import Candle, PriceSeries


@dataclass(frozen=True)
class AssetCursor:
    """Estado por activo que el worker guarda en memoria entre ciclos."""

    # Última lectura guardada cuando el proceso vio el activo por primera vez:
    # nunca se evalúan velas anteriores (evita disparar por cruces del pasado).
    floor: datetime | None
    # Última vela completa ya procesada; None al arrancar (todavía no se sabe hasta dónde se evaluó).
    evaluated_until: datetime | None = None
    # Última vela guardada en price_history; las anteriores ya están guardadas y no se reenvían.
    last_saved: datetime | None = None


@dataclass(frozen=True)
class AlertResult:
    """Resultado de evaluar una alerta: disparo (triggered) o, en una porcentual sin referencia, anclaje."""

    candle: Candle
    # Precio alcanzado (low o high) si disparó; el close de la vela si es un anclaje
    price: Decimal
    triggered: bool
    # Variación con signo (+ subió, − bajó); solo en las porcentuales que dispararon
    change_pct: Decimal | None = None


def candles_to_store(series: PriceSeries, last_saved: datetime | None) -> list[Candle]:
    """
    Velas a guardar con upsert: desde la última guardada inclusive (la vela que estaba en
    curso se actualiza). Si el activo no tenía lecturas, toda la serie del día.
    """
    if last_saved is None:
        return list(series.candles)
    return [candle for candle in series.candles if candle.timestamp >= last_saved]


def complete_candles(
    series: PriceSeries,
    interval: timedelta,
    now: datetime,
    *,
    session_closed: bool = False,
) -> list[Candle]:
    """
    Velas cerradas: su intervalo terminó antes del último dato de la serie (as_of) y antes
    de ahora. La vela que contiene as_of es la del minuto en curso y queda afuera hasta que
    cierre. Los bonos (snapshot) son lecturas puntuales y están completos.

    Con session_closed (última pasada de la ventana de gracia, rueda terminada) el límite es
    `now`: as_of queda congelado al cierre y, si no, la última vela del día nunca cerraría.
    """
    if series.snapshot:
        return list(series.candles)
    if session_closed or series.as_of is None:
        limit = now
    else:
        limit = min(series.as_of, now)
    return [candle for candle in series.candles if candle.timestamp + interval <= limit]


def select_candles_to_evaluate(
    complete: list[Candle], cursor: AssetCursor
) -> tuple[list[Candle], AssetCursor]:
    """
    Devuelve las velas completas que todavía no se evaluaron (en orden) y el cursor actualizado.

    - Cada vela completa se evalúa una sola vez: solo las posteriores a cursor.evaluated_until.
    - Arranque (sin evaluated_until): solo la última vela completa.
    - Nunca velas anteriores a cursor.floor.
    """
    if not complete:
        return [], cursor
    if cursor.evaluated_until is None:
        pending = complete[-1:]
    else:
        pending = [candle for candle in complete if candle.timestamp > cursor.evaluated_until]
    if cursor.floor is not None:
        pending = [candle for candle in pending if candle.timestamp >= cursor.floor]

    newest = complete[-1].timestamp
    if cursor.evaluated_until is not None:
        newest = max(newest, cursor.evaluated_until)
    return pending, AssetCursor(cursor.floor, newest, cursor.last_saved)


def evaluate_alert(
    alert_type: str,
    threshold: Decimal,
    reference: Decimal | None,
    created_at: datetime,
    candles: list[Candle],
) -> AlertResult | None:
    """Evalúa una alerta sobre las velas (en orden cronológico); devuelve como máximo un disparo."""
    eligible = [candle for candle in candles if candle.timestamp >= created_at]
    if not eligible:
        return None

    if alert_type == "price_min":
        hit = next((candle for candle in eligible if candle.low <= threshold), None)
        return AlertResult(hit, hit.low, True) if hit else None

    if alert_type == "price_max":
        hit = next((candle for candle in eligible if candle.high >= threshold), None)
        return AlertResult(hit, hit.high, True) if hit else None

    if alert_type == "percentage":
        if reference is None:
            # Alerta nueva: su referencia queda fija en el close de la última vela, sin disparar
            last = eligible[-1]
            return AlertResult(last, last.close, False)
        if reference <= 0:
            return None
        for candle in eligible:
            rise = (candle.high - reference) / reference * Decimal("100")
            fall = (reference - candle.low) / reference * Decimal("100")
            if max(rise, fall) >= threshold:
                # Si llegan las dos en la misma vela se usa la mayor (empate: suba)
                if rise >= fall:
                    return AlertResult(candle, candle.high, True, rise)
                return AlertResult(candle, candle.low, True, -fall)

    return None


# ── Ventana de gracia post-cierre ─────────────────────────────────────────────

# Firma de una pasada: por ticker de Yahoo, (as_of, inicio de la última vela)
SeriesSignature = dict[str, tuple[datetime | None, datetime]]
GraceDecision = Literal["continue", "stable", "deadline"]


@dataclass(frozen=True)
class GraceWindow:
    """Ventana de gracia abierta al cerrar la rueda: plazo máximo y firma de la pasada anterior."""

    deadline: datetime
    last_signature: SeriesSignature | None = None


def series_signature(series_map: Mapping[str, PriceSeries]) -> SeriesSignature:
    """Firma de las series de Yahoo (los bonos snapshot no cuentan): as_of y última vela por ticker."""
    return {
        ticker: (series.as_of, series.candles[-1].timestamp)
        for ticker, series in series_map.items()
        if not series.snapshot and series.candles
    }


def grace_decision(window: GraceWindow, signature: SeriesSignature, now: datetime) -> GraceDecision:
    """
    Qué hacer en una pasada de la ventana de gracia:

      - "stable":   la firma no está vacía y es igual a la de la pasada anterior
                    (Yahoo terminó de entregar la rueda) → cerrar las velas y terminar.
      - "deadline": venció el plazo sin estabilidad → cerrar las velas igual y terminar.
      - "continue": seguir con la próxima pasada.

    Si se cumplen las dos condiciones gana "stable" (cierre normal, sin advertencia).
    """
    if signature and signature == window.last_signature:
        return "stable"
    if now >= window.deadline:
        return "deadline"
    return "continue"
