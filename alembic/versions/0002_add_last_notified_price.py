"""add last_notified_price to alerts

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-08 10:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'alerts',
        sa.Column('last_notified_price', sa.Numeric(precision=12, scale=4), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('alerts', 'last_notified_price')

