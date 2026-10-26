"""alert_events y weekly_summaries

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-25 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Crea el historial de disparos de alertas y la caché de resúmenes semanales."""
    op.create_table('alert_events',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('alert_id', sa.Integer(), nullable=True),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('asset_id', sa.Integer(), nullable=False),
    sa.Column('alert_type', sa.String(length=20), nullable=False),
    sa.Column('threshold_value', sa.Numeric(precision=12, scale=4), nullable=False),
    sa.Column('price', sa.Numeric(precision=12, scale=4), nullable=False),
    sa.Column('change_pct', sa.Numeric(precision=9, scale=4), nullable=True),
    sa.Column('notified', sa.Boolean(), nullable=False),
    sa.Column('triggered_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['alert_id'], ['alerts.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['asset_id'], ['assets.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_alert_events_alert_id'), 'alert_events', ['alert_id'], unique=False)
    op.create_index('ix_alert_events_user_triggered', 'alert_events', ['user_id', 'triggered_at'], unique=False)
    op.create_table('weekly_summaries',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('week_start', sa.Date(), nullable=False),
    sa.Column('stats', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'week_start', name='uq_weekly_summaries_user_week')
    )


def downgrade() -> None:
    """Deshace la migración: borra las dos tablas y sus índices."""
    op.drop_table('weekly_summaries')
    op.drop_index('ix_alert_events_user_triggered', table_name='alert_events')
    op.drop_index(op.f('ix_alert_events_alert_id'), table_name='alert_events')
    op.drop_table('alert_events')
