"""watchlist_items table (docs/18 C — watchlist + poll-on-demand alerts).

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.base import JSONType, UUIDType

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "watchlist_items",
        sa.Column("id", UUIDType, primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("api_key_id", sa.Text(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("label", sa.Text()),
        sa.Column("last_hash", sa.Text()),
        sa.Column("last_urls", JSONType),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("idx_watchlist_owner", "watchlist_items", ["api_key_id"])


def downgrade() -> None:
    op.drop_index("idx_watchlist_owner", table_name="watchlist_items")
    op.drop_table("watchlist_items")
