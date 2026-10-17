"""add scheduler_state table

Revision ID: e1f2a3b4c5d6
Revises: 87ba9a4c9367
Create Date: 2026-10-18 09:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = 'e1f2a3b4c5d6'
down_revision = '87ba9a4c9367'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'scheduler_state',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('scheduler_state')
