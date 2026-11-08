"""initial schema v1

Revision ID: 0001
Revises:
Create Date: 2026-10-25

Mirrors docs/05-data-model.md. Requires the pgvector extension.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.base import EmbeddingType, JSONType, StringListType, UUIDType

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = 1024

UUID_PK = sa.text("gen_random_uuid()")
NOW = sa.text("now()")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _pg_index(*args, **kwargs) -> None:
    # HNSW vector indexes exist only on Postgres; SQLite (Lite) does without them.
    if _is_postgres():
        op.create_index(*args, **kwargs)


def upgrade() -> None:
    # Column types are the dialect-variant ones from app.db.base, so this revision also
    # builds the Lite (SQLite) schema. Postgres keeps UUID, JSONB, ARRAY and vector.
    global UUID_PK, NOW
    if _is_postgres():
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    else:
        UUID_PK = None  # ids come from the application on SQLite
        NOW = sa.text("CURRENT_TIMESTAMP")

    op.create_table(
        "users",
        sa.Column("id", UUIDType, primary_key=True, server_default=UUID_PK),
        sa.Column("email", sa.Text(), unique=True),
        sa.Column("role", sa.Text(), nullable=False, server_default="user"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", UUIDType, primary_key=True, server_default=UUID_PK),
        sa.Column("user_id", UUIDType, sa.ForeignKey("users.id")),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("name", sa.Text()),
        sa.Column("rate_limit_rpm", sa.Integer(), server_default="60"),
        sa.Column("enabled", sa.Boolean(), server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW),
    )

    op.create_table(
        "sessions",
        sa.Column("id", UUIDType, primary_key=True, server_default=UUID_PK),
        sa.Column("user_id", UUIDType, sa.ForeignKey("users.id")),
        sa.Column("title", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW),
    )

    op.create_table(
        "queries",
        sa.Column("id", UUIDType, primary_key=True, server_default=UUID_PK),
        sa.Column("session_id", UUIDType, sa.ForeignKey("sessions.id")),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("normalized_text", sa.Text()),
        sa.Column("lang", sa.Text()),
        sa.Column("intent", sa.Text()),
        sa.Column("params", JSONType),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW),
    )
    op.create_index("idx_queries_normalized", "queries", ["normalized_text"])

    op.create_table(
        "documents",
        sa.Column("id", UUIDType, primary_key=True, server_default=UUID_PK),
        sa.Column("canonical_url", sa.Text(), nullable=False, unique=True),
        sa.Column("url_hash", sa.Text(), nullable=False),
        sa.Column("title", sa.Text()),
        sa.Column("domain", sa.Text()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("content_md", sa.Text()),
        sa.Column("content_hash", sa.Text()),
        sa.Column("fetched_at", sa.DateTime(timezone=True)),
        sa.Column("fetch_status", sa.Text()),
        sa.Column("extractor_used", sa.Text()),
    )
    op.create_index("idx_documents_domain", "documents", ["domain"])

    op.create_table(
        "chunks",
        sa.Column("id", UUIDType, primary_key=True, server_default=UUID_PK),
        sa.Column(
            "document_id",
            UUIDType,
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("heading_path", sa.Text()),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer()),
        sa.Column("embedding", EmbeddingType),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW),
    )
    _pg_index(
        "idx_chunks_embedding",
        "chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_with={"m": 16, "ef_construction": 64},
    )
    op.create_index("idx_chunks_document", "chunks", ["document_id", "position"])

    op.create_table(
        "search_results",
        sa.Column("id", UUIDType, primary_key=True, server_default=UUID_PK),
        sa.Column("query_id", UUIDType, sa.ForeignKey("queries.id")),
        sa.Column("document_id", UUIDType, sa.ForeignKey("documents.id")),
        sa.Column("rank", sa.Integer()),
        sa.Column("score", sa.REAL()),
        sa.Column("consensus", sa.Integer()),
        sa.Column("found_by", StringListType),
        sa.Column("raw_snippet", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW),
    )

    op.create_table(
        "answers",
        sa.Column("id", UUIDType, primary_key=True, server_default=UUID_PK),
        sa.Column("query_id", UUIDType, sa.ForeignKey("queries.id")),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("content_md", sa.Text()),
        sa.Column("model", sa.Text()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("cost_usd", sa.REAL()),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("status", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW),
    )

    op.create_table(
        "citations",
        sa.Column("id", UUIDType, primary_key=True, server_default=UUID_PK),
        sa.Column(
            "answer_id",
            UUIDType,
            sa.ForeignKey("answers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("chunk_id", UUIDType, sa.ForeignKey("chunks.id")),
        sa.Column("url", sa.Text()),
        sa.Column("valid", sa.Boolean(), server_default="true"),
    )

    op.create_table(
        "engines",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("adapter", sa.Text(), nullable=False),
        sa.Column("weight", sa.REAL(), server_default="1.0"),
        sa.Column("enabled", sa.Boolean(), server_default="true"),
        sa.Column("timeout_ms", sa.Integer(), server_default="3000"),
        sa.Column("max_results", sa.Integer(), server_default="15"),
        sa.Column("requires_key", sa.Boolean(), server_default="false"),
        sa.Column("config", JSONType, server_default="{}"),
    )

    op.create_table(
        "engine_health",
        sa.Column("engine_id", sa.Text(), sa.ForeignKey("engines.id"), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("calls", sa.Integer()),
        sa.Column("successes", sa.Integer()),
        sa.Column("failures", sa.Integer()),
        sa.Column("p50_ms", sa.Integer()),
        sa.Column("p95_ms", sa.Integer()),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0"),
        sa.Column("disabled_reason", sa.Text()),
    )

    op.create_table(
        "semantic_cache",
        sa.Column("id", UUIDType, primary_key=True, server_default=UUID_PK),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("query_embedding", EmbeddingType),
        sa.Column("answer_id", UUIDType, sa.ForeignKey("answers.id")),
        sa.Column("model", sa.Text()),
        sa.Column("hit_count", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
    )
    _pg_index(
        "idx_semcache_emb",
        "semantic_cache",
        ["query_embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"query_embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    if _is_postgres():
        op.drop_index("idx_semcache_emb", table_name="semantic_cache")
    op.drop_table("semantic_cache")
    op.drop_table("engine_health")
    op.drop_table("engines")
    op.drop_table("citations")
    op.drop_table("answers")
    op.drop_table("search_results")
    op.drop_index("idx_chunks_document", table_name="chunks")
    if _is_postgres():
        op.drop_index("idx_chunks_embedding", table_name="chunks")
    op.drop_table("chunks")
    op.drop_index("idx_documents_domain", table_name="documents")
    op.drop_table("documents")
    op.drop_index("idx_queries_normalized", table_name="queries")
    op.drop_table("queries")
    op.drop_table("sessions")
    op.drop_table("api_keys")
    op.drop_table("users")
