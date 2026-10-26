"""restriccion unica (asset_id, timestamp) en price_history

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-26 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Elimina lecturas duplicadas y reemplaza el índice por una restricción única sobre las mismas columnas."""
    # Conserva la fila de menor id de cada (asset_id, timestamp)
    op.execute(
        """
        DELETE FROM price_history AS dup
        USING price_history AS keep
        WHERE dup.asset_id = keep.asset_id
          AND dup.timestamp = keep.timestamp
          AND dup.id > keep.id
        """
    )
    # La restricción crea su propio índice (asset_id, timestamp), que usan el gráfico y el resumen semanal
    op.create_unique_constraint('uq_price_history_asset_timestamp', 'price_history', ['asset_id', 'timestamp'])
    op.drop_index('ix_price_history_asset_timestamp', table_name='price_history')


def downgrade() -> None:
    """Vuelve al índice no único (las filas duplicadas borradas no se recuperan)."""
    op.create_index('ix_price_history_asset_timestamp', 'price_history', ['asset_id', 'timestamp'], unique=False)
    op.drop_constraint('uq_price_history_asset_timestamp', 'price_history', type_='unique')
