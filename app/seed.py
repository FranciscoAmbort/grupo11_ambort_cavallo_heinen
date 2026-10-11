"""Carga el catálogo inicial de activos. Uso: python -m app.seed"""

import asyncio

from sqlalchemy.dialects.postgresql import insert

from app.database import async_session_maker, engine
from app.models import Asset

SEED_ASSETS = [
    {"ticker": "GGAL.BA", "name": "Grupo Financiero Galicia", "asset_type": "stock", "currency": "ARS"},
    {"ticker": "YPFD.BA", "name": "YPF S.A.", "asset_type": "stock", "currency": "ARS"},
    {"ticker": "PAMP.BA", "name": "Pampa Energía", "asset_type": "stock", "currency": "ARS"},
    {"ticker": "ALUA.BA", "name": "Aluar Aluminio Argentino", "asset_type": "stock", "currency": "ARS"},
    {"ticker": "AL30", "name": "Bonos de la República Argentina USD 2030 L.A.", "asset_type": "bond", "currency": "USD"},
    {"ticker": "GD30", "name": "Bonos Globales de la República Argentina USD 2030", "asset_type": "bond", "currency": "USD"},
    {"ticker": "AE38", "name": "Bonos de la República Argentina USD 2038 L.A.", "asset_type": "bond", "currency": "USD"},
    {"ticker": "AAPL.BA", "name": "CEDEAR Apple Inc.", "asset_type": "cedear", "currency": "ARS"},
    {"ticker": "MELI.BA", "name": "CEDEAR MercadoLibre Inc.", "asset_type": "cedear", "currency": "ARS"},
    {"ticker": "TSLA.BA", "name": "CEDEAR Tesla Inc.", "asset_type": "cedear", "currency": "ARS"},
]


async def seed_assets() -> int:
    """Inserta los activos de prueba que todavía no existan y actualiza currency si ya existen."""
    insert_stmt = insert(Asset).values(SEED_ASSETS)
    stmt = insert_stmt.on_conflict_do_update(
        index_elements=["ticker"],
        set_={
            "name": insert_stmt.excluded.name,
            "asset_type": insert_stmt.excluded.asset_type,
            "currency": insert_stmt.excluded.currency,
        },
    ).returning(Asset.id)
    async with async_session_maker() as session:
        result = await session.execute(stmt)
        await session.commit()
    return len(result.all())


async def main() -> None:
    """Ejecuta la carga de activos y cierra las conexiones a la base."""
    inserted = await seed_assets()
    print(f"Activos insertados: {inserted} (ya existían: {len(SEED_ASSETS) - inserted})")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
