"""Carga el catálogo inicial de activos. Uso: python -m app.seed"""

import asyncio

from sqlalchemy.dialects.postgresql import insert

from app.database import async_session_maker, engine
from app.models import Asset

SEED_ASSETS = [
    {"ticker": "GGAL.BA", "name": "Grupo Financiero Galicia", "asset_type": "stock"},
    {"ticker": "YPFD.BA", "name": "YPF S.A.", "asset_type": "stock"},
    {"ticker": "PAMP.BA", "name": "Pampa Energía", "asset_type": "stock"},
    {"ticker": "ALUA.BA", "name": "Aluar Aluminio Argentino", "asset_type": "stock"},
    {"ticker": "AAPL.BA", "name": "CEDEAR Apple Inc.", "asset_type": "cedear"},
    {"ticker": "MELI.BA", "name": "CEDEAR MercadoLibre Inc.", "asset_type": "cedear"},
    {"ticker": "TSLA.BA", "name": "CEDEAR Tesla Inc.", "asset_type": "cedear"},
]


async def seed_assets() -> int:
    """Inserta los activos de prueba que todavía no existan y devuelve cuántos agregó."""
    stmt = insert(Asset).values(SEED_ASSETS).on_conflict_do_nothing(index_elements=["ticker"]).returning(Asset.id)
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
