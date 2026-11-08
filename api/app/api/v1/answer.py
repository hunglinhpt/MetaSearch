"""POST /v1/answer — JSON and SSE from one pipeline (docs/06-api-spec.md §2)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from app.api import deps
from app.api.v1.schemas import AnswerRequest, AnswerResponse, CitationItem
from app.core.admission import acquire_admit_slot, admit
from app.core.budget import RequestBudget, create_budget_for_route, get_current_budget, use_budget
from app.core.errors import ErrorCode, MetaSearchError
from app.llm.providers import coerce_council_endpoints
from app.orchestration.pipeline import Event, SearchParams

router = APIRouter(tags=["answer"])


class _SlotStreamResponse(EventSourceResponse):
    """SSE response that gives its admission slot back however the response ends.

    The generator's own ``finally`` only runs once the generator has started. If sending
    the response head fails first, the generator never runs, so the response releases the
    slot as well. ``AdmissionSlot.release`` is idempotent.
    """

    def __init__(self, content: Any, *, slot: Any) -> None:
        super().__init__(content)
        self._slot = slot

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._slot.release()


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
        timeout_ms=request.timeout_ms,
        include_domains=request.include_domains,
        exclude_domains=request.exclude_domains,
    )


def _extra_run_kwargs(request: AnswerRequest) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"model": request.model}
    if request.llm is not None:
        kwargs["llm_override"] = request.llm
    if request.council_mode:
        kwargs["council_mode"] = True
    if request.council_endpoints:
        kwargs["council_endpoints"] = coerce_council_endpoints(request.council_endpoints)
    if request.domain and request.domain != "auto":
        kwargs["domain"] = request.domain
    return kwargs


@router.post("/answer", response_model=AnswerResponse, response_model_exclude_none=True)
async def answer(request: AnswerRequest):
    settings = deps.get_settings()
    gateway = deps.get_gateway()
    effective_provider = getattr(gateway, "provider", settings.llm_provider)
    has_override = request.llm is not None
    is_council = (
        request.council_mode or bool(request.council_endpoints) or settings.llm_council_enabled
    )
    if not has_override and not is_council and effective_provider == "none":
        raise MetaSearchError(
            ErrorCode.INTERNAL,
            "No LLM provider configured. Set LLM_PROVIDER in environment "
            "or supply 'llm' override in request.",
            status_code=500,
        )

    budget = create_budget_for_route("answer", timeout_ms=request.timeout_ms)
    max_s = settings.answer_stream_max_s
    if not request.stream:
        async with admit("answer"):
            with use_budget(budget):
                deadline = asyncio.timeout(max_s)
                try:
                    async with deadline:
                        return await _json(_params(request), _extra_run_kwargs(request))
                except TimeoutError:
                    if deadline.expired():
                        raise MetaSearchError(
                            ErrorCode.DEADLINE_EXCEEDED,
                            f"Answer did not finish within {max_s:g}s",
                            status_code=504,
                        ) from None
                    raise

    slot = await acquire_admit_slot("answer")
    try:
        return _SlotStreamResponse(
            _sse(
                _params(request),
                _extra_run_kwargs(request),
                budget=budget,
                max_s=max_s,
            ),
            slot=slot,
        )
    except BaseException:
        slot.release()
        raise


async def _json(
    params: SearchParams,
    run_kwargs: dict[str, Any],
) -> AnswerResponse:
    answer_md_parts: list[str] = []
    citations: list[CitationItem] = []
    sources: list[dict] = []
    meta: dict = {}
    council_data: dict[str, Any] | None = None
    error: dict | None = None

    async for event, data in deps.get_answer_pipeline().run(params, **run_kwargs):
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
        elif event == "council":
            council_data = data
        elif event == "done":
            meta = data
            if council_data is None and data.get("council"):
                council_data = data["council"]
        elif event == "error":
            error = data

    if error is not None or not meta:
        budget = get_current_budget()
        if budget is not None and budget.is_expired():
            raise MetaSearchError(
                ErrorCode.DEADLINE_EXCEEDED,
                "Request deadline exceeded before answer completed",
                status_code=504,
            )
        if (error or {}).get("code") == ErrorCode.OVERLOADED.value:
            wait = math.ceil(float(error.get("retry_after_s") or 1))
            raise MetaSearchError(
                ErrorCode.OVERLOADED,
                error.get("message", "LLM providers are cooling down"),
                status_code=503,
                headers={"Retry-After": str(max(1, wait))},
            )
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
        cached=bool(meta.get("cached", False)),
        complete=bool(meta.get("complete", True)),
        council=council_data,
    )


async def _sse(
    params: SearchParams,
    run_kwargs: dict[str, Any],
    *,
    budget: RequestBudget | None = None,
    max_s: float | None = None,
) -> AsyncIterator[dict]:
    """Bridge pipeline events + mid-flight engine_done callbacks into SSE."""
    queue: asyncio.Queue[Event | None] = asyncio.Queue()

    def on_event(event: Event) -> None:
        queue.put_nowait(event)

    async def producer() -> None:
        try:
            b_ctx = use_budget(budget) if budget is not None else contextlib.nullcontext()
            with b_ctx:
                kwargs = dict(run_kwargs)
                kwargs["on_event"] = on_event
                async for event in deps.get_answer_pipeline().run(params, **kwargs):
                    await queue.put(event)
        except asyncio.CancelledError:
            # Client disconnected mid-flight; gracefully cancel
            raise
        except MetaSearchError as exc:
            await queue.put(("error", {"code": exc.code.value, "message": exc.message}))
        except Exception as exc:
            await queue.put(
                ("error", {"code": ErrorCode.INTERNAL.value, "message": str(exc)[:200]})
            )
        finally:
            await queue.put(None)

    task = asyncio.create_task(producer())
    loop = asyncio.get_running_loop()
    deadline = None if max_s is None else loop.time() + max_s
    try:
        while True:
            if deadline is None:
                event = await queue.get()
            else:
                try:
                    event = await asyncio.wait_for(queue.get(), max(0.0, deadline - loop.time()))
                except TimeoutError:
                    # Total stream ceiling: end the stream with an error the client can act on.
                    data = {
                        "code": ErrorCode.DEADLINE_EXCEEDED.value,
                        "message": f"Answer stream exceeded {max_s:g}s",
                    }
                    yield {"event": "error", "data": json.dumps(data)}
                    break
            if event is None:
                break
            name, data = event
            yield {"event": name, "data": json.dumps(data, ensure_ascii=False)}
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # The admission slot is released by _SlotStreamResponse once the response ends.
