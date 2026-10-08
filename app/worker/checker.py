"""
app/worker/checker.py
─────────────────────
Worker en segundo plano para el monitoreo periódico de cotizaciones,
persistencia de datos históricos y evaluación de alertas de usuarios.

Flujo en 3 Fases (AGENTS.md §5.1):
  - Fase 1 (Deduplicación): Obtiene los tickers únicos activos en carteras o alertas activas.
  - Fase 2 (Ingesta Concurrente): Consulta precios una sola vez por ticker usando
    app.services.market.get_multiple_prices.
  - Fase 3 (Persistencia y Evaluación):
      * Persiste cada nuevo precio en price_history.
      * Evalúa alertas activas (Umbral Fijo: One-shot; Variación Porcentual: Re-anclaje continuo).
      * Despacha notificaciones push a Telegram vía app.services.telegram.send_telegram_alert.
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import logging

from sqlalchemy import select, func, distinct
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import async_session_maker
from app.models import Asset, Alert, User, UserAsset, PriceHistory
from app.services.market import get_multiple_prices
from app.services.telegram import send_telegram_alert

logger = logging.getLogger(__name__)


async def check_market_and_evaluate_alerts() -> None:
    """
    Ejecuta un ciclo completo de chequeo de mercado y evaluación de alertas.
    Maneja su propia AsyncSession con context manager para no retener conexiones abiertas.
    """
    async with async_session_maker() as session:
        # ─────────────────────────────────────────────────────────────────────
        # FASE 1: Deduplicación de tickers a consultar
        # ─────────────────────────────────────────────────────────────────────
        # Tickers de activos que están en carteras de usuarios (user_assets)
        stmt_user_assets = (
            select(distinct(Asset.ticker))
            .join(UserAsset, UserAsset.asset_id == Asset.id)
            .where(Asset.is_active.is_(True))
        )
        res_ua = await session.execute(stmt_user_assets)
        tickers_user_assets = set(res_ua.scalars().all())

        # Tickers de activos con alertas activas
        stmt_alerts = (
            select(distinct(Asset.ticker))
            .join(Alert, Alert.asset_id == Asset.id)
            .where(Alert.is_active.is_(True), Asset.is_active.is_(True))
        )
        res_al = await session.execute(stmt_alerts)
        tickers_alerts = set(res_al.scalars().all())

        unique_tickers = list(tickers_user_assets | tickers_alerts)

        if not unique_tickers:
            logger.debug("No hay tickers seguidos ni alertas activas para consultar.")
            return

        logger.info("Fase 1: %d tickers únicos detectados para monitoreo: %s", len(unique_tickers), unique_tickers)

        # ─────────────────────────────────────────────────────────────────────
        # FASE 2: Ingesta concurrente de cotizaciones vía Yahoo Finance
        # ─────────────────────────────────────────────────────────────────────
        prices_map: dict[str, float] = await get_multiple_prices(unique_tickers)
        if not prices_map:
            logger.warning("Fase 2: No se obtuvieron cotizaciones en este ciclo.")
            return

        logger.info("Fase 2: Se obtuvieron cotizaciones para %d tickers.", len(prices_map))

        # ─────────────────────────────────────────────────────────────────────
        # FASE 3: Persistencia en price_history y Evaluación de Alertas
        # ─────────────────────────────────────────────────────────────────────
        # Traer los objetos Asset involucrados que tienen precio actualizado
        stmt_assets = select(Asset).where(Asset.ticker.in_(list(prices_map.keys())))
        res_assets = await session.execute(stmt_assets)
        assets_by_ticker = {asset.ticker: asset for asset in res_assets.scalars().all()}
        assets_by_id = {asset.id: asset for asset in assets_by_ticker.values()}

        now_utc = datetime.now(timezone.utc)

        # Guardar en price_history
        for ticker, price_float in prices_map.items():
            asset = assets_by_ticker.get(ticker)
            if asset:
                history_entry = PriceHistory(
                    asset_id=asset.id,
                    price=Decimal(str(price_float)),
                    timestamp=now_utc,
                )
                session.add(history_entry)

        # Cargar todas las alertas activas con su relación a User
        stmt_active_alerts = (
            select(Alert)
            .where(
                Alert.is_active.is_(True),
                Alert.asset_id.in_(list(assets_by_id.keys())),
            )
        )
        res_active_alerts = await session.execute(stmt_active_alerts)
        active_alerts = res_active_alerts.scalars().all()

        if not active_alerts:
            await session.commit()
            logger.debug("Fase 3: Precios históricos guardados. No hay alertas activas que evaluar.")
            return

        # Para enviar alertas requerimos el telegram_chat_id del usuario
        user_ids = {alert.user_id for alert in active_alerts}
        stmt_users = select(User).where(User.id.in_(list(user_ids)))
        res_users = await session.execute(stmt_users)
        users_by_id = {user.id: user for user in res_users.scalars().all()}

        # Evaluar cada alerta
        for alert in active_alerts:
            asset = assets_by_id.get(alert.asset_id)
            user = users_by_id.get(alert.user_id)

            if not asset or not user:
                continue

            current_price_float = prices_map.get(asset.ticker)
            if current_price_float is None:
                continue

            current_price = Decimal(str(current_price_float))
            threshold = alert.threshold_value
            alert_type = alert.alert_type

            should_trigger = False
            message = ""

            # ── 1. Alertas de Umbral Fijo (price_max / price_min) - One-Shot ────
            if alert_type == "price_max":
                if current_price >= threshold:
                    should_trigger = True
                    message = (
                        f"🎯 <b>Alerta Take Profit / Techo Superado</b>\n"
                        f"Activo: <b>{asset.ticker}</b> ({asset.name})\n"
                        f"Precio actual: <b>${current_price:,.2f} ARS</b>\n"
                        f"Umbral fijado: <b>${threshold:,.2f} ARS</b>"
                    )
            elif alert_type == "price_min":
                if current_price <= threshold:
                    should_trigger = True
                    message = (
                        f"⚠️ <b>Alerta Stop Loss / Piso Alcanzado</b>\n"
                        f"Activo: <b>{asset.ticker}</b> ({asset.name})\n"
                        f"Precio actual: <b>${current_price:,.2f} ARS</b>\n"
                        f"Umbral fijado: <b>${threshold:,.2f} ARS</b>"
                    )

            if should_trigger and alert_type in ("price_max", "price_min"):
                alert.last_triggered_at = now_utc
                alert.is_active = False  # Comportamiento One-Shot

                if user.telegram_chat_id:
                    await send_telegram_alert(user.telegram_chat_id, message, parse_mode="HTML")
                else:
                    logger.warning(
                        "Alerta %d disparada pero el usuario %s no tiene telegram_chat_id.",
                        alert.id,
                        user.email,
                    )
                continue

            # ── 2. Alertas de Variación Porcentual - Re-anclaje Continuo ───────
            if alert_type == "percentage":
                # Determinar precio de referencia
                ref_price: Decimal | None = alert.last_notified_price

                if ref_price is None:
                    # Si no tiene last_notified_price, buscar el precio anterior más reciente en price_history
                    stmt_prev_price = (
                        select(PriceHistory.price)
                        .where(PriceHistory.asset_id == asset.id, PriceHistory.timestamp < now_utc)
                        .order_by(PriceHistory.timestamp.desc())
                        .limit(1)
                    )
                    res_prev = await session.execute(stmt_prev_price)
                    prev_p = res_prev.scalar_one_or_none()
                    if prev_p is not None:
                        ref_price = prev_p
                    else:
                        # Si es el primer precio que se registra, fijarlo como referencia inicial
                        alert.last_notified_price = current_price
                        continue

                if ref_price > 0:
                    diff = current_price - ref_price
                    pct_change = (abs(diff) / ref_price) * Decimal("100")

                    if pct_change >= threshold:
                        direccion = "subió" if diff > 0 else "bajó"
                        emoji = "📈" if diff > 0 else "📉"
                        message = (
                            f"{emoji} <b>Alerta de Variación Porcentual</b>\n"
                            f"Activo: <b>{asset.ticker}</b> ({asset.name})\n"
                            f"Variación: <b>{pct_change:.2f}%</b> ({direccion})\n"
                            f"Precio actual: <b>${current_price:,.2f} ARS</b>\n"
                            f"Precio referencia anterior: <b>${ref_price:,.2f} ARS</b>\n"
                            f"Umbral: <b>{threshold:.2f}%</b>"
                        )
                        # Re-anclaje continuo
                        alert.last_triggered_at = now_utc
                        alert.last_notified_price = current_price
                        # is_active permanece True

                        if user.telegram_chat_id:
                            await send_telegram_alert(user.telegram_chat_id, message, parse_mode="HTML")
                        else:
                            logger.warning(
                                "Alerta %d disparada pero usuario %s sin telegram_chat_id.",
                                alert.id,
                                user.email,
                            )

        await session.commit()
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

