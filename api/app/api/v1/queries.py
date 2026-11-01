"""Query history: GET /v1/queries (recent) and GET /v1/queries/{id} (one).

Both endpoints degrade gracefully: when Postgres is unavailable the list
returns an empty page with ``db: false`` instead of failing — history is a
management convenience, never a request blocker.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter
from sqlalchemy import func, select

from app.api import deps
from app.core.errors import ErrorCode, MetaSearchError
from app.db.models import Answer, Citation, Query, SearchResult

router = APIRouter(tags=["queries"])


@router.get("/queries")
async def list_queries(limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """Recent queries with per-query result/answer counts (management view)."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    try:
        async with deps.get_session_factory()() as session:
            total = (await session.execute(select(func.count()).select_from(Query))).scalar_one()
            rows = (
                (
                    await session.execute(
                        select(Query).order_by(Query.created_at.desc()).offset(offset).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            ids = [q.id for q in rows]
            result_counts: dict[uuid.UUID, int] = {}
            answer_counts: dict[uuid.UUID, int] = {}
            if ids:
                for qid, cnt in (
                    await session.execute(
                        select(SearchResult.query_id, func.count())
                        .where(SearchResult.query_id.in_(ids))
                        .group_by(SearchResult.query_id)
                    )
                ).all():
                    result_counts[qid] = cnt
                for qid, cnt in (
                    await session.execute(
                        select(Answer.query_id, func.count())
                        .where(Answer.query_id.in_(ids))
                        .group_by(Answer.query_id)
                    )
                ).all():
                    answer_counts[qid] = cnt
    except Exception as exc:
        from app.core.logging import get_logger

        get_logger(__name__).warning("queries_list_db_unavailable", error=type(exc).__name__)
        return {"db": False, "total": 0, "queries": []}

    return {
        "db": True,
        "total": total,
        "queries": [
            {
                "query_id": str(q.id),
                "raw_text": q.raw_text,
                "normalized_text": q.normalized_text,
                "lang": q.lang,
                "created_at": q.created_at.isoformat() if q.created_at else None,
                "result_count": result_counts.get(q.id, 0),
                "answer_count": answer_counts.get(q.id, 0),
            }
            for q in rows
        ],
    }


@router.get("/queries/{query_id}")
async def get_query(query_id: str) -> dict[str, Any]:
    try:
        qid = uuid.UUID(query_id)
    except ValueError as exc:
        raise MetaSearchError(ErrorCode.INVALID_REQUEST, "query_id must be a UUID") from exc

    try:
        async with deps.get_session_factory()() as session:
            query = (
                await session.execute(select(Query).where(Query.id == qid))
            ).scalar_one_or_none()
            if query is None:
                raise MetaSearchError(ErrorCode.INVALID_REQUEST, "query not found", status_code=404)
            results = (
                (
                    await session.execute(
                        select(SearchResult)
                        .where(SearchResult.query_id == qid)
                        .order_by(SearchResult.rank)
                    )
                )
                .scalars()
                .all()
            )
            answers = (
                (await session.execute(select(Answer).where(Answer.query_id == qid)))
                .scalars()
                .all()
            )
            citations: dict[uuid.UUID, list[Citation]] = {}
            if answers:
                rows = (
                    (
                        await session.execute(
                            select(Citation)
                            .where(Citation.answer_id.in_([a.id for a in answers]))
                            .order_by(Citation.n)
                        )
                    )
                    .scalars()
                    .all()
                )
                for row in rows:
                    citations.setdefault(row.answer_id, []).append(row)
    except MetaSearchError:
        raise
    except Exception as exc:
        from app.core.logging import get_logger

        get_logger(__name__).warning("query_get_db_unavailable", error=type(exc).__name__)
        raise MetaSearchError(
            ErrorCode.INVALID_REQUEST, "query not found (database unavailable)", status_code=404
        ) from exc

    return {
        "query_id": str(query.id),
        "raw_text": query.raw_text,
        "normalized_text": query.normalized_text,
        "lang": query.lang,
        "intent": query.intent,
        "params": query.params,
        "created_at": query.created_at.isoformat() if query.created_at else None,
        "results": [
            {
                "rank": r.rank,
                "score": r.score,
                "consensus": r.consensus,
                "found_by": r.found_by,
                "snippet": r.raw_snippet,
            }
            for r in results
        ],
        "answers": [
            {
                "answer_id": str(a.id),
                "kind": a.kind,
                "content_md": a.content_md,
                "model": a.model,
                "latency_ms": a.latency_ms,
                "status": a.status,
                "citations": [
                    {"n": c.n, "url": c.url, "chunk_id": str(c.chunk_id or "")}
                    for c in citations.get(a.id, [])
                ],
            }
            for a in answers
        ],
    }
