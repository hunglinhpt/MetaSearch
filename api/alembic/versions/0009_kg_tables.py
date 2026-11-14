"""kg_nodes + kg_edges tables (docs/20 P2-2 — per-Space knowledge graph, v3.0.0).

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.base import UUIDType

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kg_nodes",
        sa.Column("id", UUIDType, primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("space_id", UUIDType, sa.ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("type", sa.Text()),
        sa.Column("description", sa.Text()),
        sa.UniqueConstraint("space_id", "label", name="uq_kg_node"),
    )
    op.create_index("idx_kg_nodes_space", "kg_nodes", ["space_id"])
    op.create_index("idx_kg_nodes_space_label", "kg_nodes", ["space_id", "label"])

    op.create_table(
        "kg_edges",
        sa.Column("id", UUIDType, primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("space_id", UUIDType, sa.ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("relation", sa.Text(), nullable=False),
        sa.Column("weight", sa.REAL(), nullable=False, server_default="1"),
        sa.UniqueConstraint("space_id", "source", "target", "relation", name="uq_kg_edge"),
    )
    op.create_index("idx_kg_edges_space", "kg_edges", ["space_id"])


def downgrade() -> None:
    op.drop_index("idx_kg_edges_space", table_name="kg_edges")
    op.drop_table("kg_edges")
    op.drop_index("idx_kg_nodes_space_label", table_name="kg_nodes")
    op.drop_index("idx_kg_nodes_space", table_name="kg_nodes")
    op.drop_table("kg_nodes")
