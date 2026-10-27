"""add engine health columns (Phase 2 hot config + auto-disable)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("engines", sa.Column("disabled_reason", sa.Text(), nullable=True))
    op.add_column(
        "engines",
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("engines", "consecutive_failures")
    op.drop_column("engines", "disabled_reason")
