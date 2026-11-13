"""Hybrid dense+sparse chunk retrieval over the document index (docs/20 P1-4, v2.6).

The chunk table carries a dense vector (``chunks.embedding``, pgvector on
Postgres) and a sparse column (``chunks.tsv``: real tsvector on Postgres, raw
text on SQLite). This module fuses both at chunk level with RRF, exactly like
merger.merge does at result level:

    rank_dense  = rank of cosine(query_embedding, chunk.embedding)   (if vectors exist)
    rank_sparse = rank of BM25-lite(query, chunk text)
    rrf         = 1/(k+rank_dense) + 1/(k+rank_sparse)                (missing side = no term)

The implementation is deliberately pure Python over a bounded candidate set
(latest N chunks, optional URL/domain pre-filter in SQL) so it behaves
identically on Postgres and SQLite and is unit-testable without pgvector.
When no dense vectors exist yet (embedder off), it degrades to sparse-only —
never an error.
"""

from __future__ import annotations

import math
from typing import Any

from sqlalchemy import or_, select

from app.core.logging import get_logger
from app.core.offload import run_cpu
from app.retrieval.canonical import canonicalize
from app.retrieval.scoring import tokenize

logger = get_logger(__name__)

RRF_K = 60
_CANDIDATE_LIMIT = 1000


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def bm25_scores(query: str, docs: list[str], *, k1: float = 1.5, b: float = 0.75) -> list[float]:
    """BM25-lite over pre-tokenized docs (idf + length normalization)."""
    q_terms = tokenize(query)
    if not q_terms or not docs:
        return [0.0] * len(docs)
    tokenized = [tokenize(d) for d in docs]
    avgdl = sum(len(t) for t in tokenized) / max(len(tokenized), 1)
    df: dict[str, int] = {}
    for toks in tokenized:
        for term in set(toks):
            df[term] = df.get(term, 0) + 1
    n = len(docs)
    scores: list[float] = []
    for toks in tokenized:
        if not toks:
            scores.append(0.0)
            continue
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        dl = len(toks)
        s = 0.0
        for term in q_terms:
            if term not in tf:
                continue
            idf = math.log(1.0 + (n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
            s += idf * (tf[term] * (k1 + 1)) / (tf[term] + k1 * (1 - b + b * dl / max(avgdl, 1)))
        scores.append(s)
    return scores


def rrf_fuse(dense: list[float], sparse: list[float], *, k: int = RRF_K) -> list[float]:
    """Reciprocal-rank fusion of two score lists (higher = better, both lists
    same length). A missing/None dense side contributes no term. Audit Pass 1
    P2#4: an all-zero side contributes NO term either — an empty/stopword-only
    query must not fabricate nonzero sparse ranks, and 0.0-cosine rows (no
    embedding) must not receive dense credit."""
    n = len(sparse)
    dense_live = bool(dense) and any(v > 0 for v in dense)
    sparse_live = bool(sparse) and any(v > 0 for v in sparse)
    dense_ranks = _ranks(dense) if dense_live else [n] * n
    sparse_ranks = _ranks(sparse) if sparse_live else [n] * n
    out = []
    for i in range(n):
        score = 0.0
        if sparse_live:  # each side contributes INDEPENDENTLY (Pass 2 P2#4)
            score += 1.0 / (k + sparse_ranks[i] + 1)
        if dense_live and dense[i] > 0:
            score += 1.0 / (k + dense_ranks[i] + 1)
        out.append(score)
    return out


def _ranks(scores: list[float]) -> list[int]:
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    ranks = [0] * len(scores)
    for rank, idx in enumerate(order):
        ranks[idx] = rank
    return ranks


async def hybrid_chunk_search(
    session: Any,
    query: str,
    *,
    k: int = 10,
    embedding: list[float] | None = None,
    urls: list[str] | None = None,
    domains: list[str] | None = None,
    candidate_limit: int = _CANDIDATE_LIMIT,
) -> list[dict[str, Any]]:
    """Return the top-k chunks for ``query`` fused dense+sparse (RRF).

    ``urls`` / ``domains`` pre-filter the candidate set (SQL on canonical_url /
    domain, suffix match applied in Python for domains)."""
    from app.db.models import Chunk as ChunkRow
    from app.db.models import Document

    stmt = (
        select(
            ChunkRow.id,
            ChunkRow.position,
            ChunkRow.content,
            ChunkRow.heading_path,
            ChunkRow.embedding,
            Document.canonical_url,
            Document.title,
            Document.domain,
        )
        .join(Document, Document.id == ChunkRow.document_id)
        .order_by(ChunkRow.created_at.desc())
        .limit(max(candidate_limit, k))
    )
    # Audit Pass 1 P2#3: the domain filter MUST run in SQL before the limit —
    # a Python-side-only filter on the newest-1000 window silently misses every
    # target-domain chunk outside it (wrong "no results" for Spaces).
    if domains:
        wanted_domains = []
        for d in domains:
            d = (d or "").strip().lower().rstrip(".")
            if d.startswith("www."):
                d = d[4:]
            if d:
                wanted_domains.append(d)
        if wanted_domains:
            like = [f"%.{d}" for d in wanted_domains]
            eq = list(wanted_domains)
            cond = or_(Document.domain.in_(eq), *[Document.domain.like(p) for p in like])
            stmt = stmt.where(cond)
    if urls:
        canon_urls = []
        for u in urls:
            try:
                canon_urls.append(canonicalize(u))
            except Exception:
                canon_urls.append(u)
        stmt = stmt.where(Document.canonical_url.in_(canon_urls))
    rows = (await session.execute(stmt)).all()

    # Python-side domain suffix filter (Document.domain may keep "www.").
    if domains:
        wanted = []
        for d in domains:
            d = (d or "").strip().lower().rstrip(".")
            if d.startswith("www."):
                d = d[4:]
            if d:
                wanted.append(d)
        if wanted:
            rows = [
                r
                for r in rows
                if any(
                    (d_ := (r.domain or "").lower().removeprefix("www.")) == d
                    or d_.endswith("." + d)
                    for d in wanted
                )
            ]

    if not rows:
        return []
    # Audit Pass 4 P1: BM25 + per-doc cosine over <=1000 docs is ~200-400 ms
    # of pure-Python scoring — run it OFF the event loop via run_cpu, exactly
    # like pipeline._score_chunks (AGENTS.md §4).
    ranked = await run_cpu(_score_rows_sync, rows, query, embedding)

    out: list[dict[str, Any]] = []
    for item in ranked[: max(1, min(k, 50))]:
        out.append(
            {
                "chunk_id": item["chunk_id"],
                "url": item["url"],
                "title": item["title"],
                "domain": item["domain"],
                "position": item["position"],
                "heading_path": item["heading_path"],
                "content": item["content"],
                "score": round(item["score"], 6),
                "dense": round(item["dense"], 6) if item["dense"] is not None else None,
                "sparse": round(item["sparse"], 6),
            }
        )
    return out


def _score_rows_sync(
    rows: list[Any], query: str, embedding: list[float] | None
) -> list[dict[str, Any]]:
    """Sync BM25 + cosine + RRF scoring (runs in run_cpu off the event loop)."""
    contents = [r.content or "" for r in rows]
    sparse = bm25_scores(query, contents)
    dense: list[float] | None = None
    if embedding is not None:
        dense = [
            _cosine(embedding, [float(x) for x in r.embedding]) if r.embedding else 0.0
            for r in rows
        ]
    fused = rrf_fuse(dense or [], sparse)
    order = sorted(range(len(rows)), key=lambda i: -fused[i])
    ranked: list[dict[str, Any]] = []
    for i in order:
        r = rows[i]
        ranked.append(
            {
                "chunk_id": str(r.id),
                "url": r.canonical_url,
                "title": r.title or "",
                "domain": r.domain or "",
                "position": r.position,
                "heading_path": r.heading_path or "",
                "content": r.content,
                "score": fused[i],
                "dense": dense[i] if dense else None,
                "sparse": sparse[i],
            }
        )
    return ranked
