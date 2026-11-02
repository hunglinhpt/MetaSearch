"""ORM models mirroring docs/05-data-model.md (schema v1)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    REAL,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import (
    Base,
    CreatedAtMixin,
    EmbeddingType,
    JSONType,
    StringListType,
    UUIDPrimaryKeyMixin,
    UUIDType,
)


class User(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "users"

    email: Mapped[str | None] = mapped_column(Text, unique=True)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="user")


class ApiKey(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "api_keys"

    user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, ForeignKey("users.id"))
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # Short identifier (e.g. "msk_1a2b3c4d") shown in listings; the hash is
    # the secret, never the key itself.
    key_prefix: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, server_default="user")
    rate_limit_rpm: Mapped[int] = mapped_column(Integer, server_default="60")
    max_concurrent: Mapped[int] = mapped_column(Integer, server_default="5")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="true")


class Session(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "sessions"

    user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, ForeignKey("users.id"))
    title: Mapped[str | None] = mapped_column(Text)


class Query(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "queries"
    __table_args__ = (Index("idx_queries_normalized", "normalized_text"),)

    session_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, ForeignKey("sessions.id"))
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str | None] = mapped_column(Text)
    lang: Mapped[str | None] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONType)


class Document(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (Index("idx_documents_domain", "domain"),)

    canonical_url: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    url_hash: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_md: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetch_status: Mapped[str | None] = mapped_column(Text)
    extractor_used: Mapped[str | None] = mapped_column(Text)

    chunks: Mapped[list[Chunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class Chunk(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index(
            "idx_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
        Index("idx_chunks_document", "document_id", "position"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer)
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingType)

    document: Mapped[Document] = relationship(back_populates="chunks")


class SearchResult(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "search_results"

    query_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, ForeignKey("queries.id"))
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, ForeignKey("documents.id"))
    rank: Mapped[int | None] = mapped_column(Integer)
    score: Mapped[float | None] = mapped_column(REAL)
    consensus: Mapped[int | None] = mapped_column(Integer)
    found_by: Mapped[list[str] | None] = mapped_column(StringListType)
    raw_snippet: Mapped[str | None] = mapped_column(Text)


class Answer(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "answers"

    query_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, ForeignKey("queries.id"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    content_md: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(REAL)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(Text)

    citations: Mapped[list[Citation]] = relationship(
        back_populates="answer", cascade="all, delete-orphan"
    )


class Citation(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "citations"

    answer_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("answers.id", ondelete="CASCADE"), nullable=False
    )
    n: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, ForeignKey("chunks.id"))
    url: Mapped[str | None] = mapped_column(Text)
    valid: Mapped[bool] = mapped_column(Boolean, server_default="true")

    answer: Mapped[Answer] = relationship(back_populates="citations")


class Engine(Base):
    __tablename__ = "engines"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    adapter: Mapped[str] = mapped_column(Text, nullable=False)
    weight: Mapped[float] = mapped_column(REAL, server_default="1.0")
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="true")
    timeout_ms: Mapped[int] = mapped_column(Integer, server_default="3000")
    max_results: Mapped[int] = mapped_column(Integer, server_default="15")
    requires_key: Mapped[bool] = mapped_column(Boolean, server_default="false")
    # Phase 2: health tracking + auto-disable (docs/12-roadmap-plan.md).
    disabled_reason: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, server_default="0")
    config: Mapped[dict[str, Any]] = mapped_column(JSONType, server_default="{}")


class EngineHealth(Base):
    __tablename__ = "engine_health"
    __table_args__ = (PrimaryKeyConstraint("engine_id", "window_start"),)

    engine_id: Mapped[str] = mapped_column(Text, ForeignKey("engines.id"))
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    calls: Mapped[int | None] = mapped_column(Integer)
    successes: Mapped[int | None] = mapped_column(Integer)
    failures: Mapped[int | None] = mapped_column(Integer)
    p50_ms: Mapped[int | None] = mapped_column(Integer)
    p95_ms: Mapped[int | None] = mapped_column(Integer)
    consecutive_failures: Mapped[int] = mapped_column(Integer, server_default="0")
    disabled_reason: Mapped[str | None] = mapped_column(Text)


class SemanticCache(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "semantic_cache"
    __table_args__ = (
        Index(
            "idx_semcache_emb",
            "query_embedding",
            postgresql_using="hnsw",
            postgresql_ops={"query_embedding": "vector_cosine_ops"},
        ),
    )

    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    query_embedding: Mapped[list[float] | None] = mapped_column(EmbeddingType)
    answer_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, ForeignKey("answers.id"))
    model: Mapped[str | None] = mapped_column(Text)
    hit_count: Mapped[int] = mapped_column(Integer, server_default="0")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BlogPost(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "blog_posts"
    __table_args__ = (
        Index("idx_blog_posts_slug", "slug"),
        Index("idx_blog_posts_topic", "topic"),
    )

    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str] | None] = mapped_column(StringListType)
    sources: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = [
    "Answer",
    "ApiKey",
    "BlogPost",
    "Chunk",
    "Citation",
    "Document",
    "Engine",
    "EngineHealth",
    "Query",
    "SearchResult",
    "SemanticCache",
    "Session",
    "User",
]
