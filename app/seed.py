"""Carga el catálogo inicial de activos. Uso: python -m app.seed"""

import asyncio

from sqlalchemy.dialects.postgresql import insert

from app.database import async_session_maker, engine
from app.models import Asset

SEED_ASSETS = [
    # ── Acciones Locales (BYMA) - ARS ─────────────────────────────────────────
    {"ticker": "GGAL.BA", "name": "Grupo Financiero Galicia", "asset_type": "stock", "currency": "ARS"},
    {"ticker": "YPFD.BA", "name": "YPF S.A.", "asset_type": "stock", "currency": "ARS"},
    {"ticker": "PAMP.BA", "name": "Pampa Energía", "asset_type": "stock", "currency": "ARS"},
    {"ticker": "ALUA.BA", "name": "Aluar Aluminio Argentino", "asset_type": "stock", "currency": "ARS"},
    {"ticker": "BMA.BA", "name": "Banco Macro", "asset_type": "stock", "currency": "ARS"},
    {"ticker": "TXAR.BA", "name": "Ternium Argentina", "asset_type": "stock", "currency": "ARS"},
    {"ticker": "CRES.BA", "name": "Cresud S.A.", "asset_type": "stock", "currency": "ARS"},
    {"ticker": "CEPU.BA", "name": "Central Puerto", "asset_type": "stock", "currency": "ARS"},
    {"ticker": "EDN.BA", "name": "Edenor", "asset_type": "stock", "currency": "ARS"},
    {"ticker": "BBAR.BA", "name": "BBVA Argentina", "asset_type": "stock", "currency": "ARS"},

    # ── Bonos Soberanos - USD ────────────────────────────────────────────────
    {"ticker": "AL30", "name": "Bonos de la República Argentina USD 2030 L.A.", "asset_type": "bond", "currency": "USD"},
    {"ticker": "GD30", "name": "Bonos Globales de la República Argentina USD 2030", "asset_type": "bond", "currency": "USD"},
    {"ticker": "AL35", "name": "Bonos de la República Argentina USD 2035 L.A.", "asset_type": "bond", "currency": "USD"},
    {"ticker": "GD35", "name": "Bonos Globales de la República Argentina USD 2035", "asset_type": "bond", "currency": "USD"},
    {"ticker": "AE38", "name": "Bonos de la República Argentina USD 2038 L.A.", "asset_type": "bond", "currency": "USD"},
    {"ticker": "GD38", "name": "Bonos Globales de la República Argentina USD 2038", "asset_type": "bond", "currency": "USD"},
    {"ticker": "AL41", "name": "Bonos de la República Argentina USD 2041 L.A.", "asset_type": "bond", "currency": "USD"},
    {"ticker": "GD41", "name": "Bonos Globales de la República Argentina USD 2041", "asset_type": "bond", "currency": "USD"},

    # ── CEDEARs - ARS ────────────────────────────────────────────────────────
    {"ticker": "AAPL.BA", "name": "CEDEAR Apple Inc.", "asset_type": "cedear", "currency": "ARS"},
    {"ticker": "MELI.BA", "name": "CEDEAR MercadoLibre Inc.", "asset_type": "cedear", "currency": "ARS"},
    {"ticker": "TSLA.BA", "name": "CEDEAR Tesla Inc.", "asset_type": "cedear", "currency": "ARS"},
    {"ticker": "MSFT.BA", "name": "CEDEAR Microsoft Corp.", "asset_type": "cedear", "currency": "ARS"},
    {"ticker": "NVDA.BA", "name": "CEDEAR NVIDIA Corp.", "asset_type": "cedear", "currency": "ARS"},
    {"ticker": "AMZN.BA", "name": "CEDEAR Amazon.com Inc.", "asset_type": "cedear", "currency": "ARS"},
    {"ticker": "GOOGL.BA", "name": "CEDEAR Alphabet Inc. (Google)", "asset_type": "cedear", "currency": "ARS"},
    {"ticker": "SPY.BA", "name": "CEDEAR S&P 500 ETF Trust", "asset_type": "cedear", "currency": "ARS"},
    {"ticker": "KO.BA", "name": "CEDEAR The Coca-Cola Company", "asset_type": "cedear", "currency": "ARS"},
    {"ticker": "V.BA", "name": "CEDEAR Visa Inc.", "asset_type": "cedear", "currency": "ARS"},
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
