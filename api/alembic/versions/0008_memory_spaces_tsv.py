"""memory_items + spaces tables, chunks.tsv sparse column (docs/20 P1-4/5/6, v2.6.0).

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.base import JSONType, TSVType, UUIDType

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_items",
        sa.Column("id", UUIDType, primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("api_key_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("url", sa.Text()),
        sa.Column("weight", sa.REAL(), nullable=False, server_default="0"),
        sa.Column("payload", JSONType),
    )
    op.create_index("idx_memory_owner", "memory_items", ["api_key_id"])

    op.create_table(
        "spaces",
        sa.Column("id", UUIDType, primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("api_key_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("domains", JSONType),
        sa.Column("pinned_urls", JSONType),
        sa.Column("query_terms", JSONType),
        sa.Column("restrict", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_crawl_at", sa.DateTime(timezone=True)),
    )
    op.create_index("idx_spaces_owner", "spaces", ["api_key_id"])
    op.create_index("idx_spaces_owner_slug", "spaces", ["api_key_id", "slug"], unique=True)

    # P1-4: sparse (BM25) side of the hybrid chunk index. Postgres gets a real
    # tsvector column (GIN index); SQLite gets plain text (scored in Python).
    op.add_column("chunks", sa.Column("tsv", TSVType, nullable=True))
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING gin (tsv)")
        # Backfill the sparse column for rows written before this migration.
        op.execute("UPDATE chunks SET tsv = to_tsvector('simple', content) WHERE tsv IS NULL")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_index("idx_chunks_tsv", table_name="chunks")
    op.drop_column("chunks", "tsv")
    op.drop_index("idx_spaces_owner_slug", table_name="spaces")
    op.drop_index("idx_spaces_owner", table_name="spaces")
    op.drop_table("spaces")
    op.drop_index("idx_memory_owner", table_name="memory_items")
    op.drop_table("memory_items")
