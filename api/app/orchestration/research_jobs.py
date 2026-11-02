"""Deep Research async jobs (docs/06-api-spec.md §4).

In-memory job registry (single API process, Phase 3): POST /v1/research
spawns the pipeline as a background task; GET /v1/research/{id} reads state;
GET /v1/research/{id}/events streams buffered + live events. The final report
is persisted as an Answer(kind="research") so it survives restarts; live job
state does not (documented limitation).

Concurrency cap: at most ``research_max_concurrent`` running jobs (docs/06 §8
says 5/user; single-tenant for now).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings
from app.core.errors import ErrorCode, MetaSearchError
from app.core.logging import get_logger

logger = get_logger(__name__)

_TERMINAL_EVENTS = {"done", "error"}


@dataclass(slots=True)
class ResearchJob:
    id: uuid.UUID
    query: str
    status: str = "queued"  # queued | running | done | failed
    created_at: float = field(default_factory=time.monotonic)
    progress: dict[str, int] = field(default_factory=lambda: {"iteration": 0, "max_iterations": 0})
    events: deque[tuple[int, tuple[str, dict]]] = field(default_factory=lambda: deque(maxlen=2000))
    next_seq: int = 1
    terminal: tuple[int, tuple[str, dict]] | None = None  # retained forever, never evicted
    _event_ready: asyncio.Event = field(default_factory=asyncio.Event)
    _token_batch: list[str] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    finished: bool = False

    def _flush_token_batch(self) -> None:
        if self._token_batch:
            batched_t = "".join(self._token_batch)
            self._token_batch.clear()
            seq = self.next_seq
            self.next_seq += 1
            self.events.append((seq, ("report_token", {"t": batched_t})))

    def push(self, event: tuple[str, dict]) -> None:
        """Buffer an event with a monotonically increasing sequence.

        Terminal events finalize the job.
        """
        name, data = event
        if name == "iteration_start":
            self.progress["iteration"] = int(data.get("iteration", 0))
            self.progress["max_iterations"] = int(data.get("max_iterations", 0))

        if name == "report_token":
            # When deque is near capacity (>= 1600 events), batch tokens in groups of 16
            if len(self.events) >= 1600:
                self._token_batch.append(data.get("t", ""))
                if len(self._token_batch) >= 16:
                    self._flush_token_batch()
                return
        else:
            # Flush any pending batched tokens before pushing non-token events
            self._flush_token_batch()

        seq = self.next_seq
        self.next_seq += 1
        item = (seq, event)
        self.events.append(item)

        if name in _TERMINAL_EVENTS:
            self.finished = True
            self.terminal = item
            self.status = "done" if name == "done" else "failed"
            if name == "error":
                self.error = str(data.get("message", "unknown"))

    def notify_sync(self) -> None:
        """Synchronously signal all event listeners without spawning unbounded tasks."""
        self._event_ready.set()

    async def notify(self) -> None:
        """Async compatibility wrapper."""
        self.notify_sync()
        async with self.condition:
            self.condition.notify_all()

    async def wait_event(
        self, after_seq: int, poll_after_s: float
    ) -> tuple[int, tuple[str, dict] | None]:
        """Wait for the next event past ``after_seq`` (or job end).

        If ``after_seq`` was evicted from the deque, returns ("gap", {"dropped": n}).
        If all events up to terminal were seen and job is finished, returns (after_seq, None).
        """
        deadline = time.monotonic() + poll_after_s

        while True:
            # 1. If deque has items, check if after_seq was evicted from history
            if self.events:
                oldest_seq = self.events[0][0]
                newest_seq = self.events[-1][0]

                if after_seq < oldest_seq - 1:
                    # Subscriber fell behind and missed events due to deque cap
                    dropped = (oldest_seq - 1) - max(0, after_seq)
                    gap_seq = oldest_seq - 1
                    return gap_seq, ("gap", {"dropped": dropped})

                # Check if there is an event with seq > after_seq in deque
                if after_seq < newest_seq:
                    for s, ev in self.events:
                        if s > after_seq:
                            return s, ev

            # 2. Check terminal event if no more events in deque
            if self.terminal is not None and after_seq < self.terminal[0]:
                return self.terminal[0], self.terminal[1]

            # 3. If job is finished and no more events exist past after_seq
            if self.finished:
                return after_seq, None

            # 4. Wait for new events
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return after_seq, None

            self._event_ready.clear()
            # Double check in case an event arrived just before clearing
            if self.finished or (self.events and self.events[-1][0] > after_seq) or self.terminal:
                continue

            try:
                await asyncio.wait_for(
                    self._event_ready.wait(), timeout=min(remaining, poll_after_s)
                )
            except TimeoutError:
                return after_seq, None


class ResearchJobStore:
    def __init__(self) -> None:
        self._jobs: dict[uuid.UUID, ResearchJob] = {}
        self._lock = asyncio.Lock()

    async def create(self, query: str) -> ResearchJob:
        settings = get_settings()
        async with self._lock:
            running = sum(1 for j in self._jobs.values() if j.status in ("queued", "running"))
            if running >= settings.research_max_concurrent:
                raise MetaSearchError(
                    ErrorCode.RATE_LIMITED,
                    "too many concurrent research jobs; retry later",
                    details={"max_concurrent": settings.research_max_concurrent},
                )
            job = ResearchJob(id=uuid.uuid4(), query=query)
            self._jobs[job.id] = job
            # Prune finished jobs older than an hour to bound memory.
            now = time.monotonic()
            for job_id in [
                jid for jid, j in self._jobs.items() if j.finished and now - j.created_at > 3600
            ]:
                self._jobs.pop(job_id, None)
            return job

    async def get(self, research_id: str) -> ResearchJob:
        try:
            job_id = uuid.UUID(research_id)
        except ValueError as exc:
            raise MetaSearchError(ErrorCode.INVALID_REQUEST, "research_id must be a UUID") from exc
        job = self._jobs.get(job_id)
        if job is not None:
            return job

        restored = await self._load_from_db(job_id)
        if restored is not None:
            self._jobs[job_id] = restored
            return restored

        raise MetaSearchError(
            ErrorCode.INVALID_REQUEST,
            "research job not found",
            status_code=404,
        )

    async def _load_from_db(self, job_id: uuid.UUID) -> ResearchJob | None:
        try:
            from sqlalchemy import select

            from app.db.models import (
                Answer as AnswerRow,
            )
            from app.db.models import (
                Citation as CitationRow,
            )
            from app.db.models import (
                Query as QueryRow,
            )
            from app.db.session import get_sessionmaker

            sessionmaker = get_sessionmaker()
            async with sessionmaker() as session:
                query_row = (
                    await session.execute(select(QueryRow).where(QueryRow.id == job_id))
                ).scalar_one_or_none()
                if query_row is None:
                    return None

                answer_row = (
                    await session.execute(
                        select(AnswerRow)
                        .where(
                            AnswerRow.query_id == job_id,
                            AnswerRow.kind == "research",
                        )
                        .order_by(AnswerRow.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if answer_row is None:
                    return None

                citation_rows = (
                    (
                        await session.execute(
                            select(CitationRow)
                            .where(CitationRow.answer_id == answer_row.id)
                            .order_by(CitationRow.n)
                        )
                    )
                    .scalars()
                    .all()
                )

                citations = [
                    {"n": c.n, "url": c.url or "", "title": "", "chunk_id": str(c.chunk_id or "")}
                    for c in citation_rows
                ]

                job = ResearchJob(
                    id=job_id,
                    query=query_row.raw_text,
                    status="done"
                    if answer_row.status in ("complete", "truncated")
                    else answer_row.status,
                    finished=True,
                    result={
                        "report_md": answer_row.content_md or "",
                        "citations": citations,
                        "learnings": [],
                        "cost_usd": float(answer_row.cost_usd or 0.0),
                    },
                )
                job.push(
                    (
                        "done",
                        {
                            "research_id": str(job_id),
                            "status": job.status,
                            "model": answer_row.model,
                        },
                    )
                )
                return job
        except Exception as exc:
            logger.warning("research_job_restore_failed", error=type(exc).__name__)
            return None


_job_store: ResearchJobStore | None = None


def get_job_store() -> ResearchJobStore:
    global _job_store
    if _job_store is None:
        _job_store = ResearchJobStore()
    return _job_store


def reset_job_store() -> None:
    global _job_store
    _job_store = None


def push_event(job: ResearchJob, event: tuple[str, dict]) -> None:
    """Pipeline on_event callback: buffer + wake SSE subscribers synchronously."""
    job.push(event)
    job.notify_sync()


async def run_job(job: ResearchJob, params) -> None:
    """Drive the research pipeline, forwarding events into the job.

    Captures the report/citations into ``job.result`` as they stream past so
    GET /v1/research/{id} can serve the finished report. Never raises.
    """
    from app.api.deps import get_answer_pipeline
    from app.config import get_settings
    from app.orchestration.research import ResearchPipeline

    job.status = "running"
    if getattr(params, "research_id", None) is None:
        params.research_id = job.id
    pipeline = ResearchPipeline(
        get_settings(),
        get_answer_pipeline().search,
        get_answer_pipeline().scraper,
        get_answer_pipeline().gateway,
    )
    report_parts: list[str] = []
    citations: list[dict] = []
    learnings_acc: list[str] = []
    cost_usd: float = 0.0
    try:
        async for name, data in pipeline.run(params, on_event=lambda e: push_event(job, e)):
            if name == "report_token":
                report_parts.append(data["t"])
            elif name == "citations":
                citations = data["citations"]
            elif name == "learnings":
                learnings_acc.extend(data.get("learnings", []))
            elif name == "done":
                cost_usd = float(data.get("usage", {}).get("cost_usd", 0.0) or 0.0)
                job.result = {
                    "report_md": "".join(report_parts),
                    "citations": citations,
                    "learnings": learnings_acc,
                    "cost_usd": cost_usd,
                }
            push_event(job, (name, data))
        if not job.result:
            job.result = {
                "report_md": "".join(report_parts),
                "citations": citations,
                "learnings": learnings_acc,
                "cost_usd": cost_usd,
            }
        logger.info("research_job_done", research_id=str(job.id), status=job.status)
    except asyncio.CancelledError:
        job.push(("error", {"code": "internal", "message": "research cancelled"}))
        job.notify_sync()
        raise
    except Exception as exc:
        logger.warning("research_job_failed", error=type(exc).__name__)
        job.push(("error", {"code": "internal", "message": f"{type(exc).__name__}: {exc}"[:200]}))
        job.notify_sync()
