"""Tests de app/api/assets.py con una sesión simulada (sin base de datos)."""

import asyncio
from types import SimpleNamespace

from sqlalchemy.sql.dml import Delete

from app.api.assets import remove_my_asset

USER_ID = 7
ASSET_ID = 42


class FakeSession:
    """Registra las sentencias ejecutadas y los commits, sin conectarse a PostgreSQL."""

    def __init__(self):
        self.statements = []
        self.commits = 0

    async def execute(self, statement):
        self.statements.append(statement)

    async def commit(self):
        self.commits += 1


def _sql(statement) -> str:
    """SQL de la sentencia con los parámetros ya reemplazados."""
    return str(statement.compile(compile_kwargs={"literal_binds": True}))


def test_remove_my_asset_borra_alertas_y_cartera_en_una_transaccion():
    session = FakeSession()
    user = SimpleNamespace(id=USER_ID)

    response = asyncio.run(remove_my_asset(asset_id=ASSET_ID, current_user=user, session=session))

    assert response.status_code == 204
    assert session.commits == 1
    assert len(session.statements) == 2
    assert all(isinstance(stmt, Delete) for stmt in session.statements)

    # Primero las alertas del usuario sobre ese activo, después la fila de la cartera
    alerts_stmt, user_assets_stmt = session.statements
    assert alerts_stmt.table.name == "alerts"
    assert user_assets_stmt.table.name == "user_assets"
    for stmt in session.statements:
        sql = _sql(stmt)
        assert f"{stmt.table.name}.user_id = {USER_ID}" in sql
        assert f"{stmt.table.name}.asset_id = {ASSET_ID}" in sql
