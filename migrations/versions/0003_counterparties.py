"""create counterparties table (desk-managed buyer/supplier contact book)

Revision ID: 0003_counterparties
Revises: 0002_processed_inbound
Create Date: 2026-10-17 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0003_counterparties'
down_revision: Union[str, None] = '0002_processed_inbound'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'counterparties',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('role', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('email', sa.String(), nullable=False),
        sa.Column('country', sa.String(), nullable=True),
        sa.Column('port', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('role', 'email', name='uq_counterparties_role_email'),
    )


def downgrade() -> None:
    op.drop_table('counterparties')
