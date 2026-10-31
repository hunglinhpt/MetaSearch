"""add api key role + prefix (Phase 3 auth, docs/01 FR-14)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("role", sa.Text(), nullable=False, server_default="user"),
    )
    op.add_column("api_keys", sa.Column("key_prefix", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("api_keys", "key_prefix")
    op.drop_column("api_keys", "role")
