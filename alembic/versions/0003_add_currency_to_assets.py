"""add currency to assets

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-11 16:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'assets',
        sa.Column('currency', sa.String(length=10), server_default='ARS', nullable=False)
    )


def downgrade() -> None:
    op.drop_column('assets', 'currency')

