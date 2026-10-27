"""GET /v1/queries/{id} — one stored query for debug/eval (docs/06-api-spec.md §6)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from app.api import deps
from app.core.errors import ErrorCode, MetaSearchError
from app.db.models import Answer, Citation, Query, SearchResult

router = APIRouter(tags=["queries"])


@router.get("/queries/{query_id}")
async def get_query(query_id: str) -> dict[str, Any]:
    try:
        qid = uuid.UUID(query_id)
    except ValueError as exc:
        raise MetaSearchError(ErrorCode.INVALID_REQUEST, "query_id must be a UUID") from exc

    async with deps.get_session_factory()() as session:
        query = (await session.execute(select(Query).where(Query.id == qid))).scalar_one_or_none()
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
            (await session.execute(select(Answer).where(Answer.query_id == qid))).scalars().all()
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
