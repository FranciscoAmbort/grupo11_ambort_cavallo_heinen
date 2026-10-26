"""
app/services/weekly_summary.py
──────────────────────────────
Resumen semanal de la actividad de la cartera de un usuario, con conclusiones
redactadas por la IA (app/services/ai.py) y guardadas en weekly_summaries.

La semana se presenta de lunes a viernes (los días de rueda); la ventana de datos va
de lunes 00:00 hasta el lunes siguiente 00:00 en hora argentina, y las consultas usan
ese rango convertido a UTC. week_offset 0 es la semana en curso (parcial, hasta ahora)
y 1 la semana pasada.

Las estadísticas se resuelven con tres consultas agregadas (precios por activo,
conteo de disparos por tipo y detalle de disparos), sin loops por activo ni traer
los puntos a Python. La armada del diccionario de estadísticas y del prompt son
funciones puras, sin base de datos, para poder testearlas.

A la IA se le mandan solo tickers, nombres de activos y números: ninguna función
de armado recibe datos del usuario.

Funciones públicas:
  week_range(week_offset, now=None)                      → WeekRange
  build_stats(week, asset_rows, alert_counts, events)    → dict
  build_prompt(stats)                                    → str
  has_activity(stats)                                    → bool
  is_summary_current(week_offset, created_at, week)      → bool   (el GET lo puede mostrar)
  is_cache_valid(week_offset, created_at, week, now)     → bool   (el POST no regenera)
  get_cached_summary(session, user_id, week_offset)      → SummaryResult | None
  generate_weekly_summary(session, user_id, week_offset) → SummaryResult
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
import logging
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AlertEvent, Asset, PriceHistory, UserAsset, WeeklySummary
from app.schemas import ASSET_TYPES
from app.services.ai import AIServiceError, ai_is_configured, generate_text

logger = logging.getLogger(__name__)

WEEK_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
VALID_WEEK_OFFSETS = (0, 1)
# Tope del detalle de disparos que se guarda y se manda a la IA (los conteos son exactos)
EVENTS_DETAIL_LIMIT = 50
# La semana en curso se puede regenerar recién cuando el resumen guardado tiene más de 1 hora
CACHE_TTL_CURRENT_WEEK = timedelta(hours=1)

ALERT_TYPES = ("percentage", "price_max", "price_min")
ALERT_TYPE_LABELS = {
    "percentage": "variación porcentual",
    "price_max": "precio máximo (take profit)",
    "price_min": "precio mínimo (stop loss)",
}
ASSET_TYPE_LABELS = {"stock": "acción", "bond": "bono", "cedear": "CEDEAR"}

NO_ACTIVITY_MESSAGE = (
    "No hay cotizaciones registradas ni alertas disparadas en esta semana. "
    "Los precios se guardan solo durante el horario de rueda: cuando el monitor registre "
    "lecturas de tus activos vas a poder generar el resumen."
)
AI_DISABLED_ERROR = "El análisis con IA no está habilitado en este servidor."
AI_FAILED_ERROR = "El análisis con IA no está disponible en este momento. Probá de nuevo más tarde."

SYSTEM_INSTRUCTION = """\
Sos un analista que resume la actividad semanal de una cartera de inversiones del mercado argentino
(acciones de BYMA, bonos soberanos y CEDEARs).
Escribí en castellano rioplatense, con un tono claro y profesional.
Basate únicamente en los datos que recibís en el mensaje. No inventes precios, noticias, causas ni ningún
otro dato que no esté ahí. Si un activo figura "sin datos", mencionalo sin especular.
No hagas recomendaciones de compra ni de venta, ni sugieras operar.
Cada precio está en la moneda indicada para su activo (ARS o USD): no conviertas ni compares montos entre monedas.
Extensión máxima: unas 250 palabras.
Respondé en texto plano, sin Markdown (sin asteriscos, numerales ni negritas). Organizá la respuesta en estas
cuatro secciones, en este orden, cada una con su título solo en una línea y el texto debajo:
Panorama general
Lo más destacado
Alertas de la semana
Para tener en cuenta
Si no hubo alertas, decilo en "Alertas de la semana".
Las cotizaciones solo existen de lunes a viernes, en horario de rueda: no menciones sábados ni domingos."""


# ── Tipos ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WeekRange:
    """
    Semana que se presenta de lunes a viernes en hora argentina, con sus límites en UTC para
    las consultas: la ventana de datos llega hasta el lunes siguiente 00:00.
    """

    start: date  # lunes
    end: date  # viernes, último día de rueda (solo para mostrar; las consultas usan end_utc)
    start_utc: datetime  # lunes 00:00 AR
    end_utc: datetime  # lunes siguiente 00:00 AR (límite excluyente)
    partial: bool  # True para la semana en curso


class AssetWeekRow(NamedTuple):
    """Precios agregados de un activo de la cartera en la semana (Decimal o None si no hay lecturas)."""

    ticker: str
    name: str
    asset_type: str
    currency: str
    first_price: Decimal | None
    last_price: Decimal | None
    min_price: Decimal | None
    max_price: Decimal | None
    readings: int


class AlertEventRow(NamedTuple):
    """Un disparo de alerta de la semana."""

    ticker: str
    alert_type: str
    threshold_value: Decimal
    price: Decimal
    change_pct: Decimal | None
    triggered_at: datetime


@dataclass
class SummaryResult:
    """Resultado de pedir un resumen; tiene los mismos campos que WeeklySummaryOut."""

    week_start: date
    week_end: date
    stats: dict[str, Any]
    content: str | None
    error: str | None
    model: str | None
    created_at: datetime
    from_cache: bool


# ── Funciones puras ───────────────────────────────────────────────────────────

def week_range(week_offset: int, now: datetime | None = None) -> WeekRange:
    """Calcula la semana pedida (0 = actual, 1 = pasada) respecto de `now` (por defecto, ahora)."""
    if week_offset not in VALID_WEEK_OFFSETS:
        raise ValueError(f"week_offset debe ser uno de {VALID_WEEK_OFFSETS}")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now tiene que tener zona horaria")

    today = now.astimezone(WEEK_TZ).date()
    monday = today - timedelta(days=today.weekday(), weeks=week_offset)
    next_monday = monday + timedelta(days=7)
    return WeekRange(
        start=monday,
        end=monday + timedelta(days=4),
        start_utc=datetime.combine(monday, time.min, tzinfo=WEEK_TZ).astimezone(timezone.utc),
        end_utc=datetime.combine(next_monday, time.min, tzinfo=WEEK_TZ).astimezone(timezone.utc),
        partial=week_offset == 0,
    )


def _round(value: Decimal | None, places: int) -> float | None:
    """Redondea un Decimal y lo pasa a float para el JSON (JSONB no acepta Decimal)."""
    if value is None:
        return None
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def _pct_change(first: Decimal | None, last: Decimal | None) -> Decimal | None:
    """Variación porcentual entre el primer y el último precio; None si no se puede calcular."""
    if first is None or last is None or first == 0:
        return None
    return (last - first) / first * Decimal("100")


def build_stats(
    week: WeekRange,
    asset_rows: Sequence[AssetWeekRow],
    alert_counts: Mapping[str, int],
    event_rows: Sequence[AlertEventRow],
) -> dict[str, Any]:
    """Arma el diccionario de estadísticas de la semana (es lo que se guarda y se manda a la IA)."""
    assets: list[dict[str, Any]] = []
    ranked: list[tuple[Decimal, str]] = []
    for row in asset_rows:
        has_data = row.readings > 0 and row.first_price is not None and row.last_price is not None
        pct = _pct_change(row.first_price, row.last_price) if has_data else None
        assets.append({
            "ticker": row.ticker,
            "name": row.name,
            "asset_type": row.asset_type,
            "currency": row.currency,
            "has_data": has_data,
            "first_price": _round(row.first_price, 4) if has_data else None,
            "last_price": _round(row.last_price, 4) if has_data else None,
            "min_price": _round(row.min_price, 4) if has_data else None,
            "max_price": _round(row.max_price, 4) if has_data else None,
            "change_pct": _round(pct, 2),
            "readings": int(row.readings),
        })
        if pct is not None:
            ranked.append((pct, row.ticker))

    best = worst = None
    if ranked:
        best_pct, best_ticker = max(ranked, key=lambda item: item[0])
        worst_pct, worst_ticker = min(ranked, key=lambda item: item[0])
        best = {"ticker": best_ticker, "change_pct": _round(best_pct, 2)}
        if worst_ticker != best_ticker:
            worst = {"ticker": worst_ticker, "change_pct": _round(worst_pct, 2)}

    by_type = {alert_type: 0 for alert_type in ALERT_TYPES}
    by_type.update({alert_type: int(count) for alert_type, count in alert_counts.items()})
    total = sum(by_type.values())

    events = [
        {
            "ticker": event.ticker,
            "alert_type": event.alert_type,
            "threshold": _round(event.threshold_value, 4),
            "price": _round(event.price, 4),
            "change_pct": _round(event.change_pct, 2),
            "triggered_at": event.triggered_at.astimezone(WEEK_TZ).strftime("%d/%m/%Y %H:%M"),
        }
        for event in sorted(event_rows, key=lambda event: event.triggered_at)
    ]

    return {
        "week": {"start": week.start.isoformat(), "end": week.end.isoformat(), "partial": week.partial},
        "assets": assets,
        "alerts": {
            "total": total,
            "by_type": by_type,
            "events": events,
            "events_truncated": total > len(events),
        },
        "totals": {
            "assets_count": len(assets),
            "assets_with_data": sum(1 for asset in assets if asset["has_data"]),
            "best": best,
            "worst": worst,
        },
    }


def has_activity(stats: Mapping[str, Any]) -> bool:
    """Indica si la semana tiene lecturas de precio o alertas disparadas."""
    return stats["totals"]["assets_with_data"] > 0 or stats["alerts"]["total"] > 0


def _fmt_date(iso_date: str) -> str:
    """'2026-10-19' → '19/10/2026'."""
    return date.fromisoformat(iso_date).strftime("%d/%m/%Y")


def _fmt_price(value: float) -> str:
    return f"{value:.2f}"


def _fmt_pct(value: float) -> str:
    return f"{value:+.2f}%"


def build_prompt(stats: Mapping[str, Any]) -> str:
    """Construye el mensaje para la IA a partir de las estadísticas (solo tickers, nombres y números)."""
    week = stats["week"]
    state = "semana en curso, datos hasta ahora" if week["partial"] else "semana cerrada"
    lines = [
        f"Semana del lunes {_fmt_date(week['start'])} al viernes {_fmt_date(week['end'])} ({state}).",
        "Las cotizaciones se registran solo durante el horario de rueda. Horarios en hora de Argentina.",
        "",
        f"ACTIVOS DE LA CARTERA ({len(stats['assets'])})",
    ]

    if not stats["assets"]:
        lines.append("La cartera no tiene activos.")
    for asset in stats["assets"]:
        line = (
            f"- {asset['ticker']} | {asset['name']} | "
            f"{ASSET_TYPE_LABELS.get(asset['asset_type'], asset['asset_type'])} | {asset['currency']}"
        )
        if not asset["has_data"]:
            lines.append(f"{line} | sin datos en la semana")
            continue
        change = _fmt_pct(asset["change_pct"]) if asset["change_pct"] is not None else "no calculable"
        readings = asset["readings"]
        lines.append(
            f"{line} | primer precio {_fmt_price(asset['first_price'])} | último {_fmt_price(asset['last_price'])}"
            f" | mínimo {_fmt_price(asset['min_price'])} | máximo {_fmt_price(asset['max_price'])}"
            f" | variación semanal {change} | {readings} {'lectura' if readings == 1 else 'lecturas'}"
        )

    totals = stats["totals"]
    lines += ["", "MEJOR Y PEOR DE LA SEMANA"]
    if totals["best"] is None:
        lines.append("No hay variaciones para comparar.")
    else:
        lines.append(f"- Mejor: {totals['best']['ticker']} ({_fmt_pct(totals['best']['change_pct'])})")
        if totals["worst"] is not None:
            lines.append(f"- Peor: {totals['worst']['ticker']} ({_fmt_pct(totals['worst']['change_pct'])})")

    alerts = stats["alerts"]
    lines += ["", f"ALERTAS DISPARADAS ({alerts['total']})"]
    if alerts["total"] == 0:
        lines.append("No se disparó ninguna alerta.")
    else:
        by_type = ", ".join(
            f"{ALERT_TYPE_LABELS.get(alert_type, alert_type)} {count}"
            for alert_type, count in alerts["by_type"].items()
        )
        lines.append(f"Por tipo: {by_type}.")
        if alerts["events_truncated"]:
            lines.append(
                f"Se detallan los {len(alerts['events'])} disparos más recientes de un total de {alerts['total']}."
            )
        for event in alerts["events"]:
            is_pct = event["alert_type"] == "percentage"
            threshold = f"{event['threshold']:.2f}%" if is_pct else _fmt_price(event["threshold"])
            line = (
                f"- {event['triggered_at']} | {event['ticker']} | "
                f"{ALERT_TYPE_LABELS.get(event['alert_type'], event['alert_type'])} | "
                f"umbral {threshold} | precio {_fmt_price(event['price'])}"
            )
            if event["change_pct"] is not None:
                line += f" | variación {_fmt_pct(event['change_pct'])}"
            lines.append(line)

    return "\n".join(lines)


def is_summary_current(week_offset: int, created_at: datetime, week: WeekRange) -> bool:
    """
    Indica si un resumen guardado corresponde a la semana pedida y se puede mostrar.

    Semana en curso: siempre. Semana pasada: solo si se generó con la semana ya cerrada
    (si se generó a mitad de semana, como "esta semana", la misma fila tiene datos parciales).
    """
    return week_offset == 0 or created_at >= week.end_utc


def is_cache_valid(week_offset: int, created_at: datetime, week: WeekRange, now: datetime) -> bool:
    """
    Indica si un resumen guardado se puede devolver al pedir generarlo, sin llamar a la IA.

    Semana en curso: mientras tenga menos de 1 hora. Semana pasada: si está vigente
    (generada con la semana cerrada), ya no cambia.
    """
    if week_offset == 0:
        return now - created_at < CACHE_TTL_CURRENT_WEEK
    return is_summary_current(week_offset, created_at, week)


# ── Acceso a la base ──────────────────────────────────────────────────────────

async def fetch_week_stats(session: AsyncSession, user_id: int, week: WeekRange) -> dict[str, Any]:
    """Consulta los precios y disparos de la semana del usuario y arma las estadísticas."""
    in_week = and_(
        PriceHistory.asset_id == Asset.id,
        PriceHistory.timestamp >= week.start_utc,
        PriceHistory.timestamp < week.end_utc,
    )

    def _edge_price(*order_by):
        """Primer o último precio de la semana para el activo de la fila externa."""
        return (
            select(PriceHistory.price)
            .where(in_week)
            .order_by(*order_by)
            .limit(1)
            .correlate(Asset)
            .scalar_subquery()
        )

    agg = (
        select(
            func.min(PriceHistory.price).label("min_price"),
            func.max(PriceHistory.price).label("max_price"),
            func.count().label("readings"),
        )
        .where(in_week)
        .correlate(Asset)
        .lateral("agg")
    )

    prices_stmt = (
        select(
            Asset.ticker,
            Asset.name,
            Asset.asset_type,
            Asset.currency,
            _edge_price(PriceHistory.timestamp.asc(), PriceHistory.id.asc()).label("first_price"),
            _edge_price(PriceHistory.timestamp.desc(), PriceHistory.id.desc()).label("last_price"),
            agg.c.min_price,
            agg.c.max_price,
            agg.c.readings,
        )
        .join(UserAsset, UserAsset.asset_id == Asset.id)
        .join(agg, true())
        .where(UserAsset.user_id == user_id, Asset.asset_type.in_(ASSET_TYPES))
        .order_by(Asset.ticker.asc())
    )

    events_in_week = (
        AlertEvent.user_id == user_id,
        AlertEvent.triggered_at >= week.start_utc,
        AlertEvent.triggered_at < week.end_utc,
    )
    counts_stmt = (
        select(AlertEvent.alert_type, func.count())
        .where(*events_in_week)
        .group_by(AlertEvent.alert_type)
    )
    events_stmt = (
        select(
            Asset.ticker,
            AlertEvent.alert_type,
            AlertEvent.threshold_value,
            AlertEvent.price,
            AlertEvent.change_pct,
            AlertEvent.triggered_at,
        )
        .join(Asset, Asset.id == AlertEvent.asset_id)
        .where(*events_in_week)
        .order_by(AlertEvent.triggered_at.desc(), AlertEvent.id.desc())
        .limit(EVENTS_DETAIL_LIMIT)
    )

    asset_rows = [AssetWeekRow(*row) for row in (await session.execute(prices_stmt)).all()]
    alert_counts = {alert_type: count for alert_type, count in (await session.execute(counts_stmt)).all()}
    event_rows = [AlertEventRow(*row) for row in (await session.execute(events_stmt)).all()]
    return build_stats(week, asset_rows, alert_counts, event_rows)


async def _load_summary(session: AsyncSession, user_id: int, week: WeekRange) -> WeeklySummary | None:
    """Busca el resumen guardado del usuario para esa semana."""
    return await session.scalar(
        select(WeeklySummary).where(WeeklySummary.user_id == user_id, WeeklySummary.week_start == week.start)
    )


def _cached_result(summary: WeeklySummary, week: WeekRange) -> SummaryResult:
    """Convierte un resumen guardado en el resultado que devuelve la API."""
    return SummaryResult(
        week_start=week.start,
        week_end=week.end,
        stats=summary.stats,
        content=summary.content,
        error=None,
        model=summary.model,
        created_at=summary.created_at,
        from_cache=True,
    )


async def get_cached_summary(
    session: AsyncSession,
    user_id: int,
    week_offset: int,
    now: datetime | None = None,
) -> SummaryResult | None:
    """Devuelve el resumen guardado de esa semana si existe y está vigente (ver is_summary_current)."""
    week = week_range(week_offset, now)
    summary = await _load_summary(session, user_id, week)
    if summary is None or not is_summary_current(week_offset, summary.created_at, week):
        return None
    return _cached_result(summary, week)


async def generate_weekly_summary(session: AsyncSession, user_id: int, week_offset: int) -> SummaryResult:
    """
    Devuelve el resumen de la semana: el guardado si sigue siendo válido o uno nuevo.

    Sin datos ni alertas no se llama a la IA. Si la IA no está configurada o falla, se
    devuelven igual las estadísticas con content=None y un error legible. Solo los
    resúmenes generados por la IA se guardan en caché.
    """
    now = datetime.now(timezone.utc)
    week = week_range(week_offset, now)
    saved = await _load_summary(session, user_id, week)
    if saved is not None and is_cache_valid(week_offset, saved.created_at, week, now):
        return _cached_result(saved, week)

    stats = await fetch_week_stats(session, user_id, week)
    # Cierra la transacción de lectura para no retener una conexión durante la llamada a la IA
    await session.commit()

    def _uncached(content: str | None, error: str | None) -> SummaryResult:
        return SummaryResult(week.start, week.end, stats, content, error, None, now, False)

    if not has_activity(stats):
        return _uncached(NO_ACTIVITY_MESSAGE, None)
    if not ai_is_configured():
        return _uncached(None, AI_DISABLED_ERROR)

    model = settings.gemini_model
    try:
        content = await generate_text(build_prompt(stats), system_instruction=SYSTEM_INSTRUCTION)
    except AIServiceError as exc:
        logger.warning("No se pudo generar el resumen semanal con IA: %s", exc)
        return _uncached(None, AI_FAILED_ERROR)

    insert_stmt = insert(WeeklySummary).values(
        user_id=user_id, week_start=week.start, stats=stats, content=content, model=model
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        constraint="uq_weekly_summaries_user_week",
        set_={
            "stats": insert_stmt.excluded.stats,
            "content": insert_stmt.excluded.content,
            "model": insert_stmt.excluded.model,
            "created_at": func.now(),
        },
    ).returning(WeeklySummary.created_at)
    created_at = (await session.execute(upsert_stmt)).scalar_one()
    await session.commit()

    return SummaryResult(week.start, week.end, stats, content, None, model, created_at, False)
