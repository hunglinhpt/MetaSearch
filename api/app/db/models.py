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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import (
    Base,
    CreatedAtMixin,
    EmbeddingType,
    JSONType,
    StringListType,
    TSVType,
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
    # FIX1: migration 0004 already created this column; the model lacked it, so
    # persisting an answer raised TypeError and answers were silently lost.
    api_key_id: Mapped[str | None] = mapped_column(Text)
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
    # docs/20 P1-4: sparse side of the hybrid index (tsvector on Postgres,
    # plain text on SQLite — see TSVType). Nullable so old rows stay valid.
    tsv: Mapped[str | None] = mapped_column(TSVType)

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
    # FIX1: see Query.api_key_id — column exists in migration 0004.
    api_key_id: Mapped[str | None] = mapped_column(Text)
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


class WatchlistItem(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One watched query per API key (docs/18 C).

    ``last_urls`` / ``last_hash`` are the alert baseline: a hash of the top
    result URLs seen at the last check. Alerts are poll-on-demand — the client
    calls GET /v1/watchlist/alerts and the endpoint diffs against the baseline,
    then advances it. Nothing here is ever fabricated: the URLs come from the
    search pipeline's real results.
    """

    __tablename__ = "watchlist_items"
    __table_args__ = (Index("idx_watchlist_owner", "api_key_id"),)

    api_key_id: Mapped[str] = mapped_column(Text, nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    last_hash: Mapped[str | None] = mapped_column(Text)
    last_urls: Mapped[list[str] | None] = mapped_column(JSONType)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KeyBoost(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One per-key rerank preference (docs/20 P0-6 — Goggles-lite).

    ``kind`` is "domain" (boost/demote a host suffix) or "keyword" (boost a
    phrase in the query). Applied in merger.merge AFTER scoring, only for the
    owning key, and folded into the search cache key so boosted results never
    leak into another key's cache entry.
    """

    __tablename__ = "key_boosts"
    __table_args__ = (Index("idx_key_boosts_owner", "api_key_id"),)

    api_key_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # domain | keyword
    target: Mapped[str] = mapped_column(Text, nullable=False)
    weight: Mapped[float] = mapped_column(REAL, nullable=False, server_default="0.5")


class MemoryItem(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One per-key memory entry (docs/20 P1-5 — Mem0-lite, v2.6).

    ``kind`` is "query" (a past query, for future contextualization) or
    "feedback" (per-URL 👍/👎, converted into rerank boost rules by
    ``app.retrieval.memory.load_memory_boosts``). Tenancy by ``api_key_id``;
    the owner can wipe everything with DELETE /v1/memory (privacy).
    """

    __tablename__ = "memory_items"
    __table_args__ = (Index("idx_memory_owner", "api_key_id"),)

    api_key_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # query | feedback
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    url: Mapped[str | None] = mapped_column(Text)
    weight: Mapped[float] = mapped_column(REAL, nullable=False, server_default="0")
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONType)


class Space(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A named collection (docs/20 P1-6 — Perplexity-Spaces-lite, v2.6).

    A space scopes search to ``domains`` (host suffixes) and boosts
    ``pinned_urls``; ``restrict``=true drops results outside the scope
    (include-domain filter semantics). Per-key, capped per owner.
    """

    __tablename__ = "spaces"
    __table_args__ = (
        Index("idx_spaces_owner", "api_key_id"),
        Index("idx_spaces_owner_slug", "api_key_id", "slug", unique=True),
    )

    api_key_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    domains: Mapped[list[str] | None] = mapped_column(JSONType)
    pinned_urls: Mapped[list[str] | None] = mapped_column(JSONType)
    query_terms: Mapped[list[str] | None] = mapped_column(JSONType)
    restrict: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_crawl_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KgNode(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One entity in a Space's knowledge graph (docs/20 P2-2 — LightRAG-lite, v3.0).

    Nodes/edges are extracted from the space's crawled documents by a cheap LLM
    call and are scoped to (space, owner)."""

    __tablename__ = "kg_nodes"
    __table_args__ = (
        Index("idx_kg_nodes_space", "space_id"),
        Index("idx_kg_nodes_space_label", "space_id", "label"),
        # One row per (space, label): repeated builds must not duplicate nodes.
        UniqueConstraint("space_id", "label", name="uq_kg_node"),
    )

    space_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False
    )
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)


class KgEdge(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One relation between two KgNode labels (source/target are labels)."""

    __tablename__ = "kg_edges"
    __table_args__ = (
        Index("idx_kg_edges_space", "space_id"),
        # One row per (space, source, target, relation): idempotent rebuilds.
        UniqueConstraint("space_id", "source", "target", "relation", name="uq_kg_edge"),
    )

    space_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    relation: Mapped[str] = mapped_column(Text, nullable=False)
    weight: Mapped[float] = mapped_column(REAL, nullable=False, server_default="1")


__all__ = [
    "Answer",
    "ApiKey",
    "BlogPost",
    "Chunk",
    "Citation",
    "Document",
    "Engine",
    "EngineHealth",
    "KeyBoost",
    "KgEdge",
    "KgNode",
    "MemoryItem",
    "Query",
    "SearchResult",
    "SemanticCache",
    "Session",
    "Space",
    "User",
    "WatchlistItem",
]
