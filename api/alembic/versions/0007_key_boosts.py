"""key_boosts table (docs/20 P0-6 — per-key custom rerank boosts, v2.5.0).

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.base import UUIDType

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "key_boosts",
        sa.Column("id", UUIDType, primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("api_key_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("weight", sa.REAL(), nullable=False, server_default="0.5"),
    )
    op.create_index("idx_key_boosts_owner", "key_boosts", ["api_key_id"])


def downgrade() -> None:
    op.drop_index("idx_key_boosts_owner", table_name="key_boosts")
    op.drop_table("key_boosts")
