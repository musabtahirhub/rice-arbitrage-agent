"""create campaigns and trade_audits tables

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-25 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0001_initial'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'campaigns',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('commodity', sa.String(), nullable=False),
        sa.Column('target_volume_mt', sa.Float(), nullable=False),
        sa.Column('destination_port', sa.String(), nullable=False),
        sa.Column('origin_port_default', sa.String(), nullable=False),
        sa.Column('deal_status', sa.String(), nullable=False),
        sa.Column('anchor_cif_usd', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'trade_audits',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('campaign_id', sa.String(), nullable=False),
        sa.Column('thread_id', sa.String(), nullable=False),
        sa.Column('role', sa.String(), nullable=False),
        sa.Column('counterparty_price', sa.Float(), nullable=True),
        sa.Column('net_spread', sa.Float(), nullable=True),
        sa.Column('raw_message', sa.Text(), nullable=False),
        sa.Column('direction', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_trade_audits_thread_id'), 'trade_audits', ['thread_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_trade_audits_thread_id'), table_name='trade_audits')
    op.drop_table('trade_audits')
    op.drop_table('campaigns')
