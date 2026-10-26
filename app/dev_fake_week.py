"""
app/dev_fake_week.py
────────────────────
SOLO PARA DESARROLLO. Carga lecturas de precio y disparos de alerta FALSOS para
probar el resumen semanal sin esperar una semana de ruedas reales.

Para el usuario indicado inserta, en los activos de su cartera, una lectura
sintética cada 30 minutos de 11:00 a 17:00 (hora argentina) en los días hábiles
de los últimos 7 días, y algunos alert_events. También borra los resúmenes
guardados de esas semanas, para poder regenerarlos con los datos nuevos.

Es seguro correrlo varias veces: no duplica lecturas ni eventos que ya existan
en los mismos horarios.

Uso:
  python -m app.dev_fake_week <email>          → solo muestra lo que haría
  python -m app.dev_fake_week <email> --yes    → escribe en la base

Por seguridad, se niega a correr contra una base que no sea local (db, localhost
o 127.0.0.1), salvo que se agregue --allow-remote-db.
"""

import argparse
import asyncio
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
import random
import sys

from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url

from app.config import settings
from app.database import async_session_maker, engine
from app.models import AlertEvent, Asset, PriceHistory, User, UserAsset, WeeklySummary
from app.services.weekly_summary import WEEK_TZ

LOCAL_DB_HOSTS = ("db", "localhost", "127.0.0.1")

DAYS_BACK = 7
FIRST_SLOT = time(11, 0)
LAST_SLOT = time(17, 0)
SLOT_STEP = timedelta(minutes=30)

# Variación máxima entre dos lecturas seguidas (±0,6 %)
MAX_STEP_CHANGE = 0.006
# Precio inicial si el activo no tiene ninguna lectura anterior
DEFAULT_START_PRICE = {"bond": Decimal("60"), "stock": Decimal("1000"), "cedear": Decimal("1000")}
PRICE_QUANTUM = Decimal("0.0001")


def build_slots(now: datetime) -> list[datetime]:
    """Horarios de 11:00 a 17:00 cada 30 min, en días hábiles de los últimos 7 días, anteriores a `now` (en UTC)."""
    today = now.astimezone(WEEK_TZ).date()
    slots: list[datetime] = []
    for days_ago in range(DAYS_BACK - 1, -1, -1):
        day = today - timedelta(days=days_ago)
        if day.weekday() >= 5:  # sábado o domingo
            continue
        slot = datetime.combine(day, FIRST_SLOT, tzinfo=WEEK_TZ)
        last = datetime.combine(day, LAST_SLOT, tzinfo=WEEK_TZ)
        while slot <= last and slot < now:
            slots.append(slot.astimezone(timezone.utc))
            slot += SLOT_STEP
    return slots


def synthetic_prices(ticker: str, start_price: Decimal, slots: list[datetime]) -> list[Decimal]:
    """Camino aleatorio determinístico (misma semilla por ticker) a partir de `start_price`."""
    rng = random.Random(f"dev_fake_week:{ticker}")
    price = start_price
    prices: list[Decimal] = []
    for _ in slots:
        change = Decimal(str(rng.uniform(-MAX_STEP_CHANGE, MAX_STEP_CHANGE)))
        price = (price * (1 + change)).quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP)
        prices.append(price)
    return prices


def synthetic_events(series: list[tuple[Asset, list[Decimal]]], slots: list[datetime]) -> list[dict]:
    """
    Arma algunos disparos de alerta coherentes con las lecturas sintéticas:
    una variación porcentual por activo (hasta 3), un techo en el máximo del
    primero y un piso en el mínimo del segundo.
    """
    events: list[dict] = []
    for index, (asset, prices) in enumerate(series[:3]):
        if len(prices) < 5:
            continue
        # Variación porcentual: lectura de la mitad de la ventana contra 4 lecturas antes
        at = len(prices) // 2 + index
        ref = prices[at - 4]
        change = ((prices[at] - ref) / ref * 100).quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP)
        # Umbral coherente con el disparo: el escalón de 0,25 % inmediatamente inferior a la variación
        threshold = max(Decimal("0.1"), (abs(change) * 4).to_integral_value(rounding=ROUND_FLOOR) / 4)
        events.append({
            "asset_id": asset.id, "alert_type": "percentage", "threshold_value": threshold,
            "price": prices[at], "change_pct": change, "triggered_at": slots[at],
        })
        if index == 0:
            top = max(range(len(prices)), key=prices.__getitem__)
            events.append({
                "asset_id": asset.id, "alert_type": "price_max",
                "threshold_value": (prices[top] * Decimal("0.995")).quantize(PRICE_QUANTUM),
                "price": prices[top], "change_pct": None, "triggered_at": slots[top],
            })
        if index == 1:
            bottom = min(range(len(prices)), key=prices.__getitem__)
            events.append({
                "asset_id": asset.id, "alert_type": "price_min",
                "threshold_value": (prices[bottom] * Decimal("1.005")).quantize(PRICE_QUANTUM),
                "price": prices[bottom], "change_pct": None, "triggered_at": slots[bottom],
            })
    return events


def _monday(moment: datetime) -> date:
    """Lunes (hora argentina) de la semana de `moment`."""
    day = moment.astimezone(WEEK_TZ).date()
    return day - timedelta(days=day.weekday())


async def run(email: str, write: bool) -> int:
    """Calcula lo que hay que insertar y, si `write`, lo escribe en una sola transacción. Devuelve el código de salida."""
    now = datetime.now(timezone.utc)
    slots = build_slots(now)

    async with async_session_maker() as session:
        user = await session.scalar(select(User).where(User.email == email))
        if user is None:
            print(f"ERROR: no existe ningún usuario con el correo {email!r}.")
            return 1

        assets = (
            await session.scalars(
                select(Asset)
                .join(UserAsset, UserAsset.asset_id == Asset.id)
                .where(UserAsset.user_id == user.id)
                .order_by(Asset.ticker)
            )
        ).all()
        print(f"Usuario: {email} (id {user.id}) · cartera: {len(assets)} activos "
              f"({', '.join(asset.ticker for asset in assets) or '-'})")
        if not assets or not slots:
            print("No hay nada para cargar (cartera vacía o sin días hábiles en la ventana).")
            return 0

        first_day = slots[0].astimezone(WEEK_TZ).strftime("%d/%m")
        last_day = slots[-1].astimezone(WEEK_TZ).strftime("%d/%m/%Y")
        print(f"Ventana: {first_day} al {last_day}, días hábiles de 11:00 a 17:00 cada 30 min ({len(slots)} horarios)")

        asset_ids = [asset.id for asset in assets]

        # Último precio de cada activo anterior a la ventana, para que la serie arranque de ahí
        last_before = dict(
            (
                await session.execute(
                    select(PriceHistory.asset_id, PriceHistory.price)
                    .where(PriceHistory.asset_id.in_(asset_ids), PriceHistory.timestamp < slots[0])
                    .distinct(PriceHistory.asset_id)
                    .order_by(PriceHistory.asset_id, PriceHistory.timestamp.desc())
                )
            ).all()
        )
        existing_readings = set(
            (
                await session.execute(
                    select(PriceHistory.asset_id, PriceHistory.timestamp)
                    .where(PriceHistory.asset_id.in_(asset_ids), PriceHistory.timestamp.in_(slots))
                )
            ).all()
        )

        series: list[tuple[Asset, list[Decimal]]] = []
        new_readings: list[dict] = []
        for asset in assets:
            start = last_before.get(asset.id) or DEFAULT_START_PRICE.get(asset.asset_type, Decimal("1000"))
            prices = synthetic_prices(asset.ticker, start, slots)
            series.append((asset, prices))
            missing = [
                {"asset_id": asset.id, "price": price, "timestamp": slot}
                for slot, price in zip(slots, prices)
                if (asset.id, slot) not in existing_readings
            ]
            new_readings += missing
            print(f"  {asset.ticker:<10} {len(missing):>3} lecturas nuevas · desde {start} hasta {prices[-1]} {asset.currency}")

        events = synthetic_events(series, slots)
        existing_events = set(
            (
                await session.execute(
                    select(AlertEvent.asset_id, AlertEvent.alert_type, AlertEvent.triggered_at).where(
                        AlertEvent.user_id == user.id,
                        AlertEvent.triggered_at >= slots[0],
                        AlertEvent.triggered_at <= slots[-1],
                    )
                )
            ).all()
        )
        new_events = [
            event for event in events
            if (event["asset_id"], event["alert_type"], event["triggered_at"]) not in existing_events
        ]

        weeks = sorted({_monday(slot) for slot in slots})
        cached = await session.scalar(
            select(func.count()).select_from(WeeklySummary).where(
                WeeklySummary.user_id == user.id, WeeklySummary.week_start.in_(weeks)
            )
        )

        print(f"Lecturas a insertar: {len(new_readings)} (ya existían {len(slots) * len(assets) - len(new_readings)})")
        print(f"Disparos de alerta a insertar: {len(new_events)} (ya existían {len(events) - len(new_events)})")
        print(f"Resúmenes guardados a borrar: {cached} "
              f"(semanas del {', '.join(week.strftime('%d/%m/%Y') for week in weeks)})")

        if not write:
            print("\nVista previa: no se escribió nada. Para escribir, volvé a correrlo con --yes.")
            return 0

        if new_readings:
            session.add_all(PriceHistory(**reading) for reading in new_readings)
        if new_events:
            session.add_all(AlertEvent(user_id=user.id, alert_id=None, notified=False, **event) for event in new_events)
        await session.execute(
            delete(WeeklySummary).where(WeeklySummary.user_id == user.id, WeeklySummary.week_start.in_(weeks))
        )
        await session.commit()

    print(f"\nListo: {len(new_readings)} lecturas y {len(new_events)} disparos insertados; {cached} resúmenes borrados.")
    return 0


async def main() -> int:
    """Valida los argumentos y la base de destino antes de tocar nada."""
    parser = argparse.ArgumentParser(
        prog="python -m app.dev_fake_week",
        description="SOLO PARA DESARROLLO: carga una semana de lecturas y alertas falsas para un usuario.",
    )
    parser.add_argument("email", help="correo del usuario cuya cartera se completa")
    parser.add_argument("--yes", action="store_true", help="escribir en la base (sin esto solo muestra lo que haría)")
    parser.add_argument("--allow-remote-db", action="store_true", help="permitir una base que no sea local")
    args = parser.parse_args()

    url = make_url(settings.database_url)
    print("=" * 72)
    print("⚠  SOLO PARA DESARROLLO: este script inserta datos de precios y alertas FALSOS.")
    print("=" * 72)
    print(f"Base de datos: host={url.host} puerto={url.port or 5432} base={url.database}")

    if url.host not in LOCAL_DB_HOSTS and not args.allow_remote_db:
        print(
            f"\nERROR: la base está en {url.host!r}, que no es una base local ({', '.join(LOCAL_DB_HOSTS)}). "
            "Este script es solo para desarrollo. Si de verdad querés escribir ahí, agregá --allow-remote-db."
        )
        return 2

    try:
        return await run(args.email, write=args.yes)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
