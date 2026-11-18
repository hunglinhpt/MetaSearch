"""documents/chunks tenancy: api_key_id owner columns (docs/30 P1-2).

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable = "global/public row" (operator crawls, seed data). Rows written
    # by tenant flows carry the producing key's id and are private to it.
    op.add_column(
        "documents", sa.Column("api_key_id", sa.Text(), nullable=True)
    )
    op.add_column("chunks", sa.Column("api_key_id", sa.Text(), nullable=True))
    op.create_index("idx_documents_api_key_id", "documents", ["api_key_id"])
    op.create_index("idx_chunks_api_key_id", "chunks", ["api_key_id"])


def downgrade() -> None:
    op.drop_index("idx_chunks_api_key_id", table_name="chunks")
    op.drop_index("idx_documents_api_key_id", table_name="documents")
    op.drop_column("chunks", "api_key_id")
    op.drop_column("documents", "api_key_id")
