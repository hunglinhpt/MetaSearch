"""add query/answer ownership + key concurrency/expiry (WP-11, docs/01 FR-14)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _has_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    op.add_column("queries", sa.Column("api_key_id", sa.Text(), nullable=True))
    op.add_column("answers", sa.Column("api_key_id", sa.Text(), nullable=True))
    op.add_column(
        "api_keys", sa.Column("max_concurrent", sa.Integer(), server_default="5")
    )
    op.add_column("api_keys", sa.Column("expires_at", sa.DateTime(timezone=True)))
    # Legacy rows belong to nobody; admin-only visibility is enforced in code.
    if not _has_sqlite():
        op.create_index("idx_queries_api_key", "queries", ["api_key_id"])
        op.create_index("idx_answers_api_key", "answers", ["api_key_id"])


def downgrade() -> None:
    if not _has_sqlite():
        op.drop_index("idx_answers_api_key", table_name="answers")
        op.drop_index("idx_queries_api_key", table_name="queries")
    op.drop_column("api_keys", "expires_at")
    op.drop_column("api_keys", "max_concurrent")
    op.drop_column("answers", "api_key_id")
    op.drop_column("queries", "api_key_id")
