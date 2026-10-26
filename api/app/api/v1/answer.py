"""POST /v1/answer — JSON and SSE from one pipeline (docs/06-api-spec.md §2)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from app.api.deps import get_answer_pipeline
from app.api.v1.schemas import AnswerRequest, AnswerResponse, CitationItem
from app.core.errors import ErrorCode, MetaSearchError
from app.orchestration.pipeline import Event, SearchParams

router = APIRouter(tags=["answer"])


def _params(request: AnswerRequest) -> SearchParams:
    return SearchParams(
        query=request.query,
        lang=request.lang,
        k=request.k,
        engines=request.engines,
        categories=request.categories,
        freshness_days=request.freshness_days,
        include_snippets=request.include_snippets,
        news=request.news,
    )


@router.post("/answer", response_model=AnswerResponse, response_model_exclude_none=True)
async def answer(request: AnswerRequest):
    if request.stream:
        return EventSourceResponse(_sse(_params(request), request.model))
    return await _json(_params(request), request.model)


async def _json(params: SearchParams, model: str | None) -> AnswerResponse:
    answer_md_parts: list[str] = []
    citations: list[CitationItem] = []
    sources: list[dict] = []
    meta: dict = {}
    error: dict | None = None

    async for event, data in get_answer_pipeline().run(params, model=model):
        if event == "token":
            answer_md_parts.append(data["t"])
        elif event == "citations":
            citations = [
                CitationItem(
                    n=c["n"], url=c["url"], title=c.get("title", ""), chunk_id=c.get("chunk_id", "")
                )
                for c in data["citations"]
            ]
        elif event == "sources":
            sources = data["sources"]
        elif event == "done":
            meta = data
        elif event == "error":
            error = data

    if error is not None or not meta:
        raise MetaSearchError(
            ErrorCode.INTERNAL,
            (error or {}).get("message", "answer pipeline produced no output"),
        )

    return AnswerResponse(
        answer_id=meta["answer_id"],
        answer_md="".join(answer_md_parts),
        citations=citations,
        sources=sources,
        model=meta.get("model", ""),
        latency_ms=meta.get("latency_ms", 0),
        status=meta.get("status", "complete"),
    )


async def _sse(params: SearchParams, model: str | None) -> AsyncIterator[dict]:
    """Bridge pipeline events + mid-flight engine_done callbacks into SSE.

    A queue interleaves the pipeline's own events with ``engine_done`` events
    pushed by fan-out tasks while the search stage is still running.
    """
    queue: asyncio.Queue[Event | None] = asyncio.Queue()

    def on_event(event: Event) -> None:
        queue.put_nowait(event)

    async def producer() -> None:
        try:
            async for event in get_answer_pipeline().run(params, model=model, on_event=on_event):
                await queue.put(event)
        except MetaSearchError as exc:
            await queue.put(("error", {"code": exc.code.value, "message": exc.message}))
        except Exception as exc:
            await queue.put(
                ("error", {"code": ErrorCode.INTERNAL.value, "message": str(exc)[:200]})
            )
        finally:
            await queue.put(None)

    task = asyncio.create_task(producer())
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            name, data = event
            yield {"event": name, "data": json.dumps(data, ensure_ascii=False)}
    finally:
        task.cancel()
