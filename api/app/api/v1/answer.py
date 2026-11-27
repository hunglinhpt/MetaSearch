"""POST /v1/answer — JSON and SSE from one pipeline (docs/06-api-spec.md §2)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import APIRouter
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from app.api import deps
from app.api.v1.schemas import AnswerRequest, AnswerResponse, CitationItem
from app.core.admission import acquire_admit_slot, admit
from app.core.auth import current_api_key_id
from app.core.budget import RequestBudget, create_budget_for_route, get_current_budget, use_budget
from app.core.errors import ErrorCode, MetaSearchError
from app.core.logging import get_logger
from app.db.models import Answer, Query
from app.llm.providers import coerce_council_endpoints
from app.orchestration.pipeline import Event, SearchParams

logger = get_logger(__name__)

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
        deep=request.deep,
        max_age_s=request.max_age_s,
        space=request.space,
    )


async def _load_thread_context(thread_id: str) -> dict[str, str] | None:
    """docs/30 BP-1: load the prior turn's answer + question for follow-up
    query rewrite. Returns None when the thread_id is invalid/not owned."""
    try:
        prior_id = uuid.UUID(thread_id)
    except ValueError:
        return None
    try:
        async with deps.get_session_factory()() as session:
            row = (
                (
                    await session.execute(
                        select(Answer).where(
                            Answer.id == prior_id,
                            Answer.api_key_id == current_api_key_id.get(),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if row is None or not row.content_md:
                return None
            prior_q = ""
            if row.query_id:
                q_row = (
                    await session.execute(select(Query.raw_text).where(Query.id == row.query_id))
                ).scalar()
                prior_q = q_row or ""
            return {
                "prior_answer_id": str(row.id),
                "prior_question": prior_q[:200],
                "prior_answer_md": row.content_md[:2000],
            }
    except Exception as exc:
        logger.warning("thread_context_load_failed", error=type(exc).__name__)
        return None


def _extra_run_kwargs(request: AnswerRequest) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"model": request.model}
    if request.llm is not None:
        kwargs["llm_override"] = request.llm
    if request.council_mode:
        kwargs["council_mode"] = True
    if request.council_endpoints:
        kwargs["council_endpoints"] = coerce_council_endpoints(list(request.council_endpoints))
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
    # Fail fast when no LLM provider, fallback, override, or council is configured.
    fallback_provider = getattr(settings, "llm_fallback_provider", "") or "none"
    run_kwargs = _extra_run_kwargs(request)
    if (
        not has_override
        and not is_council
        and effective_provider == "none"
        and fallback_provider == "none"
    ):
        raise MetaSearchError(
            ErrorCode.INTERNAL,
            "No LLM provider configured. Set LLM_PROVIDER in environment "
            "or supply 'llm' override in request.",
            status_code=500,
        )

    budget = create_budget_for_route("answer", timeout_ms=request.timeout_ms)
    max_s = settings.answer_stream_max_s
    # docs/30 BP-1 (audit R2): the threaded-context DB read runs INSIDE the
    # admitted region (the answer tier bounds it like every other answer-path
    # cost) — resolved lazily by the JSON body or the SSE producer below.
    thread_id = request.thread_id
    thread_loader = (lambda: _load_thread_context(thread_id)) if thread_id else None
    if not request.stream:
        async with admit("answer"):
            with use_budget(budget):
                if thread_loader is not None:
                    thread_context = await thread_loader()
                    if thread_context:
                        run_kwargs["thread_context"] = thread_context
                deadline = asyncio.timeout(max_s)
                try:
                    async with deadline:
                        return await _json(_params(request), run_kwargs)
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
                run_kwargs,
                budget=budget,
                max_s=max_s,
                thread_loader=thread_loader,
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
    follow_ups: list[str] = []  # v2.5 P0-5
    unsupported_markers: list[int] = []  # v2.5 P0-4
    rewritten_query: str | None = None  # docs/30 BP-1 (audit R2)

    async for event, data in deps.get_answer_pipeline().run(params, **run_kwargs):
        if event == "token":
            answer_md_parts.append(data["t"])
        elif event == "thread_rewrite":
            # JSON mode: surface the substitution on the response object (SSE
            # clients see the thread_rewrite event directly).
            rewritten_query = str(data.get("rewritten") or "") or None
        elif event == "citations":
            citations = [
                CitationItem(
                    n=c["n"], url=c["url"], title=c.get("title", ""), chunk_id=c.get("chunk_id", "")
                )
                for c in data["citations"]
            ]
            unsupported_markers = [int(n) for n in data.get("unsupported_markers") or []]
        elif event == "follow_ups":
            follow_ups = [str(f) for f in data.get("follow_ups") or []]
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
        err = error or {}
        if err.get("code") == ErrorCode.OVERLOADED.value:
            wait = math.ceil(float(err.get("retry_after_s") or 1))
            raise MetaSearchError(
                ErrorCode.OVERLOADED,
                err.get("message", "LLM providers are cooling down"),
                status_code=503,
                headers={"Retry-After": str(max(1, wait))},
            )
        if err.get("code") in (ErrorCode.UPSTREAM_ERROR.value, ErrorCode.UPSTREAM_TIMEOUT.value):
            # docs/36 A0: the LLM provider failed this request — a 502, not our 500.
            raise MetaSearchError(
                ErrorCode(err["code"]),
                err.get("message", "LLM provider failed"),
                status_code=502,
            )
        raise MetaSearchError(
            ErrorCode.INTERNAL,
            (error or {}).get("message", "answer pipeline produced no output"),
        )

    return AnswerResponse(
        answer_id=meta["answer_id"],
        answer_md=str(meta.get("answer_md") or "".join(answer_md_parts)),
        citations=citations,
        follow_ups=list(meta.get("follow_ups") or follow_ups),
        unsupported_markers=unsupported_markers,
        sources=sources,
        model=meta.get("model", ""),
        latency_ms=meta.get("latency_ms", 0),
        status=meta.get("status", "complete"),
        cached=bool(meta.get("cached", False)),
        complete=bool(meta.get("complete", True)),
        rewritten_query=rewritten_query,
        council=council_data,
        skill=meta.get("skill"),
        compression=meta.get("compression"),
        rerank=meta.get("rerank"),
        grounding=meta.get("grounding"),
    )


async def _sse(
    params: SearchParams,
    run_kwargs: dict[str, Any],
    *,
    budget: RequestBudget | None = None,
    max_s: float | None = None,
    thread_loader: Callable[[], Awaitable[dict[str, str] | None]] | None = None,
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
                if thread_loader is not None:
                    thread_context = await thread_loader()
                    if thread_context:
                        kwargs["thread_context"] = thread_context
                kwargs["on_event"] = on_event
                async for event in deps.get_answer_pipeline().run(params, **kwargs):
                    await queue.put(event)
        except asyncio.CancelledError:
            # Client disconnected mid-flight; gracefully cancel
            raise
        except MetaSearchError as exc:
            await queue.put(("error", {"code": exc.code.value, "message": exc.message}))
        except Exception:
            # docs/29 P1-6: raw exception text can embed driver/URL detail -
            # clients get the stable envelope, the detail stays in logs.
            await queue.put(
                (
                    "error",
                    {
                        "code": ErrorCode.INTERNAL.value,
                        "message": "Internal error while streaming the answer.",
                    },
                )
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
