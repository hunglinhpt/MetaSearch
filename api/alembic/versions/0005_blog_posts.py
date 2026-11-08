"""blog_posts table (model existed without a migration).

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.base import JSONType, StringListType, UUIDType

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "blog_posts",
        sa.Column("id", UUIDType, primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("slug", sa.Text(), nullable=False, unique=True),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text()),
        sa.Column("content_md", sa.Text(), nullable=False),
        sa.Column("tags", StringListType),
        sa.Column("sources", JSONType),
        sa.Column("published_at", sa.DateTime(timezone=True)),
    )
    op.create_index("idx_blog_posts_slug", "blog_posts", ["slug"])
    op.create_index("idx_blog_posts_topic", "blog_posts", ["topic"])


def downgrade() -> None:
    op.drop_index("idx_blog_posts_topic", table_name="blog_posts")
    op.drop_index("idx_blog_posts_slug", table_name="blog_posts")
    op.drop_table("blog_posts")
