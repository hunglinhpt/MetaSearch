"""Schema assertions that need no live database."""

from __future__ import annotations

from app.db import models
from app.db.base import EMBEDDING_DIM, Base

EXPECTED_TABLES = {
    "answers",
    "api_keys",
    "blog_posts",
    "chunks",
    "citations",
    "documents",
    "engine_health",
    "engines",
    "queries",
    "search_results",
    "semantic_cache",
    "sessions",
    "users",
}


def test_all_v1_tables_are_declared() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_embedding_dimension_matches_docs() -> None:
    assert EMBEDDING_DIM == 1024
    embedding_type = models.Chunk.__table__.c.embedding.type
    pg_variant = embedding_type._variant_mapping.get("postgresql", embedding_type)
    assert getattr(pg_variant, "dim", EMBEDDING_DIM) == EMBEDDING_DIM
    cache_type = models.SemanticCache.__table__.c.query_embedding.type
    pg_variant = cache_type._variant_mapping.get("postgresql", cache_type)
    assert getattr(pg_variant, "dim", EMBEDDING_DIM) == EMBEDDING_DIM


def test_engine_health_composite_primary_key() -> None:
    pk = models.EngineHealth.__table__.primary_key
    assert [column.name for column in pk.columns] == ["engine_id", "window_start"]


def test_canonical_url_is_unique() -> None:
    assert models.Document.__table__.c.canonical_url.unique is True


def test_cascade_deletes_are_declared() -> None:
    assert models.Chunk.__table__.c.document_id.foreign_keys.pop().ondelete == "CASCADE"
    assert models.Citation.__table__.c.answer_id.foreign_keys.pop().ondelete == "CASCADE"


def test_expected_indexes_exist() -> None:
    names = {index.name for table in Base.metadata.tables.values() for index in table.indexes}
    assert {
        "idx_queries_normalized",
        "idx_documents_domain",
        "idx_chunks_embedding",
        "idx_chunks_document",
        "idx_semcache_emb",
    } <= names
