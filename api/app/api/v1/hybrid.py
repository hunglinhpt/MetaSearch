"""POST /v1/hybrid — hybrid dense+sparse chunk search over the document index
(docs/20 P1-4, v2.6).

Searches the persisted chunks (written by /v1/scrape-heavy flows, research,
/v1/crawl) fusing BM25 (tsv column) with dense cosine when the L2 embedder is
configured; RRF-fused at chunk level (app.retrieval.hybrid). Sparse-only when
no embeddings exist yet — never an error.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api import deps
from app.config import get_settings
from app.core.admission import admit
from app.core.auth import current_api_key_id
from app.core.budget import create_budget_for_route, use_budget
from app.core.errors import ErrorCode, MetaSearchError
from app.llm.embeddings import EmbeddingError
from app.retrieval.hybrid import hybrid_chunk_search

router = APIRouter(tags=["hybrid"])


class HybridRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=10, ge=1, le=50)
    # Restrict the candidate set: exact document URLs and/or domain suffixes,
    # or one of the caller's Spaces (domains scope from the space).
    urls: list[str] | None = Field(default=None, max_length=50)
    domains: list[str] | None = Field(default=None, max_length=20)
    space: str | None = Field(default=None, max_length=100)
    dense: bool = True  # use the dense side when the embedder is configured
    timeout_ms: int | None = Field(default=None, ge=1_000, le=60_000)


@router.post("/hybrid")
async def hybrid_search(request: HybridRequest) -> dict[str, Any]:
    settings = get_settings()
    if not settings.hybrid_search_enabled:
        raise MetaSearchError(
            ErrorCode.INVALID_REQUEST, "hybrid search is disabled", status_code=400
        )
    budget = create_budget_for_route("hybrid", timeout_ms=request.timeout_ms)
    async with admit("hybrid"):
        with use_budget(budget):
            domains = list(request.domains or [])
            if request.space:
                from app.retrieval.spaces import get_space, space_domain_scope

                owner = current_api_key_id.get() or "anonymous"
                space = await get_space(owner, request.space)
                if space is None:
                    raise MetaSearchError(
                        ErrorCode.NOT_FOUND, f"space not found: {request.space}", status_code=404
                    )
                domains.extend(space_domain_scope(space))

            embedding: list[float] | None = None
            if request.dense:
                embedder = deps.get_embedder()
                if embedder is not None and embedder.enabled:
                    try:
                        embedding = await embedder.embed(request.query)
                    except EmbeddingError:
                        embedding = None  # sparse-only fallback, never fail

            async with deps.get_session_factory()() as session:
                chunks = await hybrid_chunk_search(
                    session,
                    request.query,
                    k=request.k,
                    embedding=embedding,
                    urls=request.urls,
                    domains=domains or None,
                )
    return {
        "query": request.query,
        "k": request.k,
        "dense_used": embedding is not None,
        "chunks": chunks,
    }
