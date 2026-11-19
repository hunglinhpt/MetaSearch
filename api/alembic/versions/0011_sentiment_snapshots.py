"""sentiment_snapshots table for rolling 20d S-Score baselines (v4.0).

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.base import UUIDType

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sentiment_snapshots",
        sa.Column("id", UUIDType, primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("snapshot_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("composite_score", sa.REAL(), nullable=False),
        sa.Column("text_score", sa.REAL()),
        sa.Column("quant_score", sa.REAL()),
        sa.Column("social_volume", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mention_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("regime", sa.Text(), nullable=False),
        sa.Column("confidence", sa.REAL(), nullable=False),
        sa.UniqueConstraint("ticker", "snapshot_date", name="uq_sentiment_snapshot_ticker_day"),
    )
    op.create_index("idx_sentiment_snapshots_ticker", "sentiment_snapshots", ["ticker"])
    op.create_index(
        "idx_sentiment_snapshots_ticker_date",
        "sentiment_snapshots",
        ["ticker", "snapshot_date"],
    )


def downgrade() -> None:
    op.drop_index("idx_sentiment_snapshots_ticker_date", table_name="sentiment_snapshots")
    op.drop_index("idx_sentiment_snapshots_ticker", table_name="sentiment_snapshots")
    op.drop_table("sentiment_snapshots")
