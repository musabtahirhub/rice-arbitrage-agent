"""create processed_inbound_emails table (exactly-once inbound email handling)

Revision ID: 0002_processed_inbound
Revises: 0001_initial
Create Date: 2026-10-15 21:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0002_processed_inbound'
down_revision: Union[str, None] = '0001_initial'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'processed_inbound_emails',
        sa.Column('message_key', sa.String(), nullable=False),
        sa.Column('campaign_id', sa.String(), nullable=True),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('message_key'),
    )


def downgrade() -> None:
    op.drop_table('processed_inbound_emails')
