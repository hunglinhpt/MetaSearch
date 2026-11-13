"""Declarative base and shared column conventions.

Dialect-aware types (Lite profile, docs/08): the same models run on
Postgres+pgvector **and** SQLite — ``with_variant`` keeps Postgres on its
native types (UUID/JSONB/ARRAY/Vector) while SQLite gets portable
equivalents (CHAR uuid / JSON).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, DateTime, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Embedding width is shared with the Alembic migration so both stay in sync.
EMBEDDING_DIM = 1024

# Generic types that compile natively on both Postgres and SQLite.
# sa.Uuid renders PG "uuid" on Postgres and CHAR(32) on SQLite, binding
# uuid.UUID objects on both.
UUIDType = Uuid()
JSONType = JSON().with_variant(JSONB(), "postgresql")
StringListType = JSON().with_variant(ARRAY(String()), "postgresql")
EmbeddingType = JSON().with_variant(Vector(EMBEDDING_DIM), "postgresql")
# docs/20 P1-4: sparse (BM25) side of the hybrid chunk index. Postgres gets a
# real tsvector column; SQLite stores the raw text and the hybrid scorer
# falls back to in-Python token matching.
TSVType = Text().with_variant(TSVECTOR(), "postgresql")


class Base(DeclarativeBase):
    """Base class for all ORM models."""


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        primary_key=True,
        default=uuid.uuid4,  # client-side default: works on every dialect
    )


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
