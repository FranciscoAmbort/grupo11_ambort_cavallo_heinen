"""
app/worker/checker.py
─────────────────────
Worker en segundo plano para el monitoreo periódico de cotizaciones,
persistencia de datos históricos y evaluación de alertas de usuarios.

Antes de las fases se consulta el estado del mercado (rueda.abierta de la API de
bonos de CompararFondos). Con el mercado cerrado, o si no se puede saber, se
saltea el ciclo entero: no se consultan precios, no se guardan ni se evalúan alertas.

Flujo en 3 Fases (AGENTS.md §5.1):
  - Fase 1 (Deduplicación): Obtiene los tickers únicos activos en carteras o alertas activas.
  - Fase 2 (Ingesta Concurrente): Consulta una sola vez por ticker la serie intradiaria de
    velas (close, high, low) usando app.services.market_series.get_multiple_series.
  - Fase 3 (Persistencia y Evaluación):
      * Guarda el close de cada vela en price_history con upsert sobre (asset_id, timestamp);
        la vela del minuto en curso se actualiza en cada ciclo.
      * Evalúa las alertas activas sobre los máximos y mínimos de las velas completas que
        todavía no se evaluaron (ver app/worker/evaluation.py), para detectar cruces aunque
        el precio haya vuelto. Umbral Fijo: One-shot; Variación Porcentual: Re-anclaje continuo.
      * Despacha notificaciones push a Telegram vía app.services.telegram.send_telegram_alert.
"""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import logging
from zoneinfo import ZoneInfo

from sqlalchemy import select, func, distinct
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.database import async_session_maker
from app.models import Asset, Alert, AlertEvent, User, UserAsset, PriceHistory
from app.services.market import fetch_bonds_data, is_market_open
from app.services.market_series import Candle, PriceSeries, get_multiple_series
from app.services.email import send_email_alert
from app.services.telegram import send_telegram_alert
from app.worker.evaluation import (
    AlertResult,
    AssetCursor,
    candles_to_store,
    complete_candles,
    evaluate_alert,
    select_candles_to_evaluate,
)

logger = logging.getLogger(__name__)

# Último estado del mercado visto por el worker (None hasta el primer ciclo).
# Sirve para loguear solo los cambios de abierto a cerrado y viceversa.
_last_market_open: bool | None = None

# Por activo (asset_id): hasta qué vela se evaluó y se guardó en este proceso.
# Vive en memoria: al reiniciar, cada activo vuelve a evaluar solo su última vela completa.
_cursors: dict[int, AssetCursor] = {}

# Hora de la vela que se muestra en las notificaciones
_AR_TZ = ZoneInfo("America/Argentina/Buenos_Aires")

# Filas por sentencia en el upsert de precios
_UPSERT_BATCH_SIZE = 5000


def _log_market_state(is_open: bool) -> None:
    """Registra en el log el estado del mercado solo cuando cambia respecto del ciclo anterior."""
    global _last_market_open
    if is_open == _last_market_open:
        return
    if is_open:
        logger.info("Mercado abierto: el worker reanuda la consulta de precios y la evaluación de alertas.")
    else:
        logger.info("Mercado cerrado: el worker saltea los ciclos hasta que vuelva a abrir.")
    _last_market_open = is_open


async def _notify_user(user: User, asset: Asset, alert_id: int, message: str) -> bool:
    """Avisa al usuario por Telegram y, si no lo tiene vinculado o el envío falla, por correo."""
    if user.telegram_chat_id:
        if await send_telegram_alert(user.telegram_chat_id, message, parse_mode="HTML"):
            return True
        logger.warning("Alerta %d: falló Telegram para %s; se intenta por correo.", alert_id, user.email)

    if await send_email_alert(user.email, f"Alerta de precio: {asset.ticker}", message):
        return True

    logger.warning("Alerta %d disparada pero no se pudo avisar a %s por ningún canal.", alert_id, user.email)
    return False


async def _upsert_prices(session, rows: list[dict]) -> None:
    """Guarda el close de cada vela; si la vela ya existe (la del minuto en curso), actualiza su precio."""
    for start in range(0, len(rows), _UPSERT_BATCH_SIZE):
        stmt = insert(PriceHistory).values(rows[start:start + _UPSERT_BATCH_SIZE])
        stmt = stmt.on_conflict_do_update(
            constraint="uq_price_history_asset_timestamp",
            set_={"price": stmt.excluded.price},
            # Sin cambios de precio no se reescribe la fila
            where=PriceHistory.price.is_distinct_from(stmt.excluded.price),
        )
        await session.execute(stmt)


def _trigger_message(alert: Alert, asset: Asset, result: AlertResult) -> str:
    """Texto de la notificación: precio alcanzado y hora (Argentina) de la vela en que se cruzó el límite."""
    curr = getattr(asset, "currency", "ARS") or "ARS"
    when = result.candle.timestamp.astimezone(_AR_TZ).strftime("%H:%M")
    price = result.price
    threshold = alert.threshold_value

    if alert.alert_type == "price_max":
        return (
            f"🎯 <b>Alerta Take Profit / Techo Superado</b>\n"
            f"Activo: <b>{asset.ticker}</b> ({asset.name})\n"
            f"Precio alcanzado: <b>${price:,.2f} {curr}</b> a las {when} (hora Argentina)\n"
            f"Umbral fijado: <b>${threshold:,.2f} {curr}</b>"
        )
    if alert.alert_type == "price_min":
        return (
            f"⚠️ <b>Alerta Stop Loss / Piso Alcanzado</b>\n"
            f"Activo: <b>{asset.ticker}</b> ({asset.name})\n"
            f"Precio alcanzado: <b>${price:,.2f} {curr}</b> a las {when} (hora Argentina)\n"
            f"Umbral fijado: <b>${threshold:,.2f} {curr}</b>"
        )

    rose = result.change_pct is not None and result.change_pct > 0
    return (
        f"{'📈' if rose else '📉'} <b>Alerta de Variación Porcentual</b>\n"
        f"Activo: <b>{asset.ticker}</b> ({asset.name})\n"
        f"Variación: <b>{abs(result.change_pct or 0):.2f}%</b> ({'subió' if rose else 'bajó'}) "
        f"a las {when} (hora Argentina)\n"
        f"Precio alcanzado: <b>${price:,.2f} {curr}</b>\n"
        f"Precio referencia anterior: <b>${alert.last_notified_price:,.2f} {curr}</b>\n"
        f"Umbral: <b>{threshold:.2f}%</b>"
    )


async def check_market_and_evaluate_alerts() -> None:
    """
    Ejecuta un ciclo completo de chequeo de mercado y evaluación de alertas.
    Maneja su propia AsyncSession con context manager para no retener conexiones abiertas.
    Si el mercado está cerrado (o no se puede saber) no hace nada en este ciclo.
    """
    # ─────────────────────────────────────────────────────────────────────
    # ESTADO DEL MERCADO: una sola petición a /api/bonos por ciclo, que se
    # reutiliza después para los precios de los bonos en la Fase 2.
    # ─────────────────────────────────────────────────────────────────────
    bonds_data = await fetch_bonds_data()
    market_open = is_market_open(bonds_data)
    if market_open is None:
        logger.warning(
            "No se pudo obtener el estado del mercado (rueda.abierta) desde CompararFondos; "
            "se trata como mercado cerrado y se saltea el ciclo."
        )
        market_open = False

    _log_market_state(market_open)
    if not market_open:
        return

    async with async_session_maker() as session:
        # ─────────────────────────────────────────────────────────────────────
        # FASE 1: Deduplicación de tickers a consultar (ticker y asset_type)
        # ─────────────────────────────────────────────────────────────────────
        # Activos en carteras de usuarios (user_assets)
        stmt_user_assets = (
            select(distinct(Asset.ticker), Asset.asset_type)
            .join(UserAsset, UserAsset.asset_id == Asset.id)
            .where(Asset.is_active.is_(True))
        )
        res_ua = await session.execute(stmt_user_assets)
        assets_map: dict[str, str] = dict(res_ua.all())

        # Activos con alertas activas
        stmt_alerts = (
            select(distinct(Asset.ticker), Asset.asset_type)
            .join(Alert, Alert.asset_id == Asset.id)
            .where(Alert.is_active.is_(True), Asset.is_active.is_(True))
        )
        res_al = await session.execute(stmt_alerts)
        assets_map.update(dict(res_al.all()))

        if not assets_map:
            logger.debug("No hay tickers seguidos ni alertas activas para consultar.")
            return

        logger.info(
            "Fase 1: %d activos únicos detectados para monitoreo: %s",
            len(assets_map),
            list(assets_map.keys()),
        )

        # ─────────────────────────────────────────────────────────────────────
        # FASE 2: Ingesta concurrente de series de velas (Multi-Proveedor)
        # ─────────────────────────────────────────────────────────────────────
        series_map: dict[str, PriceSeries] = await get_multiple_series(
            assets_map, bonds_data=bonds_data, interval=settings.yahoo_interval
        )
        if not series_map:
            logger.warning("Fase 2: No se obtuvieron cotizaciones en este ciclo.")
            return

        logger.info("Fase 2: Se obtuvieron series de precios para %d tickers.", len(series_map))

        # ─────────────────────────────────────────────────────────────────────
        # FASE 3: Persistencia en price_history y Evaluación de Alertas
        # ─────────────────────────────────────────────────────────────────────
        # Traer los objetos Asset involucrados que tienen serie de precios
        stmt_assets = select(Asset).where(Asset.ticker.in_(list(series_map.keys())))
        res_assets = await session.execute(stmt_assets)
        assets_by_ticker = {asset.ticker: asset for asset in res_assets.scalars().all()}
        assets_by_id = {asset.id: asset for asset in assets_by_ticker.values()}

        now_utc = datetime.now(timezone.utc)

        # Activos que este proceso todavía no vio: su piso es la última lectura guardada,
        # para no evaluar nunca velas anteriores (por ejemplo, las previas al deploy).
        unseen_ids = [asset_id for asset_id in assets_by_id if asset_id not in _cursors]
        if unseen_ids:
            stmt_last = (
                select(PriceHistory.asset_id, func.max(PriceHistory.timestamp))
                .where(PriceHistory.asset_id.in_(unseen_ids))
                .group_by(PriceHistory.asset_id)
            )
            last_by_id = dict((await session.execute(stmt_last)).all())
            for asset_id in unseen_ids:
                last = last_by_id.get(asset_id)
                _cursors[asset_id] = AssetCursor(floor=last, last_saved=last)

        # Velas a guardar (upsert) y velas completas pendientes de evaluar, por activo.
        # Los cursores nuevos se aplican recién después del commit.
        rows: list[dict] = []
        pending_by_asset: dict[int, list[Candle]] = {}
        next_cursors: dict[int, AssetCursor] = {}
        for ticker, series in series_map.items():
            asset = assets_by_ticker.get(ticker)
            if asset is None:
                continue
            cursor = _cursors[asset.id]
            to_store = candles_to_store(series, cursor.last_saved)
            rows += [{"asset_id": asset.id, "price": c.close, "timestamp": c.timestamp} for c in to_store]
            complete = complete_candles(series, settings.yahoo_interval_delta, now_utc)
            pending, cursor = select_candles_to_evaluate(complete, cursor)
            if to_store:
                cursor = replace(cursor, last_saved=to_store[-1].timestamp)
            next_cursors[asset.id] = cursor
            if pending:
                pending_by_asset[asset.id] = pending

        await _upsert_prices(session, rows)

        # Cargar las alertas activas de los activos con velas nuevas para evaluar
        active_alerts = []
        if pending_by_asset:
            stmt_active_alerts = select(Alert).where(
                Alert.is_active.is_(True),
                Alert.asset_id.in_(list(pending_by_asset.keys())),
            )
            active_alerts = (await session.execute(stmt_active_alerts)).scalars().all()

        if not active_alerts:
            await session.commit()
            _cursors.update(next_cursors)
            logger.debug("Fase 3: %d velas guardadas. No hay alertas activas que evaluar.", len(rows))
            return

        # Para enviar alertas requerimos el telegram_chat_id del usuario
        user_ids = {alert.user_id for alert in active_alerts}
        stmt_users = select(User).where(User.id.in_(list(user_ids)))
        res_users = await session.execute(stmt_users)
        users_by_id = {user.id: user for user in res_users.scalars().all()}

        # Evaluar cada alerta sobre las velas completas que todavía no se evaluaron
        for alert in active_alerts:
            asset = assets_by_id.get(alert.asset_id)
            user = users_by_id.get(alert.user_id)
            if not asset or not user:
                continue

            result = evaluate_alert(
                alert.alert_type,
                alert.threshold_value,
                alert.last_notified_price,
                alert.created_at,
                pending_by_asset[alert.asset_id],
            )
            if result is None:
                continue

            if not result.triggered:
                # Porcentual nueva: su referencia queda fija en esta vela hasta que avise y se re-ancle
                alert.last_notified_price = result.price
                continue

            message = _trigger_message(alert, asset, result)
            triggered_at = result.candle.timestamp
            alert.last_triggered_at = triggered_at
            if alert.alert_type == "percentage":
                alert.last_notified_price = result.price  # Re-anclaje continuo; is_active permanece True
            else:
                alert.is_active = False  # Umbral fijo: comportamiento One-Shot

            notified = await _notify_user(user, asset, alert.id, message)
            # Historial del disparo (se commitea junto con el resto del ciclo)
            change_pct = result.change_pct
            session.add(AlertEvent(
                alert_id=alert.id, user_id=alert.user_id, asset_id=alert.asset_id,
                alert_type=alert.alert_type, threshold_value=alert.threshold_value, price=result.price,
                # Numeric(9, 4): un valor fuera de rango haría fallar el commit de todo el ciclo
                change_pct=change_pct if change_pct is not None and abs(change_pct) < 100000 else None,
                notified=notified, triggered_at=triggered_at,
            ))

        await session.commit()
        _cursors.update(next_cursors)
        logger.info("Fase 3: Persistencia y evaluación completadas exitosamente.")


async def run_checker_loop(interval_seconds: int = 60) -> None:
    """
    Bucle asíncrono infinito que ejecuta el ciclo de monitoreo en segundo plano.
    Captura excepciones para no tumbar la tarea y respeta cancelaciones limpias.
    """
    logger.info("Iniciando background worker de monitoreo (intervalo: %ds)...", interval_seconds)
    while True:
        try:
            await check_market_and_evaluate_alerts()
        except asyncio.CancelledError:
            logger.info("Background worker cancelado. Finalizando ejecución limpiamente.")
            break
        except Exception as exc:
            logger.exception("Error durante la ejecución del ciclo del worker: %s", exc)

        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            logger.info("Background worker cancelado durante espera. Finalizando ejecución.")
            break

