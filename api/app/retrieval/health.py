"""Engine circuit breaker and health tracking in-memory (WP-03).

Provides zero-IO, synchronous circuit breaking per search engine:
- States: closed, open, half_open (for single trial).
- Cooldown: 30s base, exponential backoff up to 15 min (900s), with jitter.
- Honors Retry-After for rate limits (Outcome.RATE_LIMITED).
- Local overload and cancellation do NOT count as failures.
- Fail-open fallback when all candidate engines are tripped.
- Asynchronous batch background writer for persisting health metrics.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


class Outcome(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    UPSTREAM_ERROR = "upstream_error"
    RATE_LIMITED = "rate_limited"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"
    LOCAL_OVERLOAD = "local_overload"
    CANCELLED = "cancelled"


@dataclass
class EngineBreaker:
    threshold: int = 3
    timeout_threshold: int = 5
    state: str = "closed"  # "closed" | "open" | "half_open"
    consecutive_failures: int = 0
    consecutive_timeouts: int = 0
    retry_at: float = 0.0
    cooldown_s: float = 30.0
    max_cooldown_s: float = 900.0
    half_open_in_flight: bool = False
    half_open_since: float = 0.0
    half_open_deadline_s: float = 30.0

    def allow(self, now: float | None = None) -> bool:
        if now is None:
            now = time.monotonic()

        if self.state == "closed":
            return True

        if self.state == "open":
            if now >= self.retry_at:
                self.state = "half_open"
                self.half_open_in_flight = True
                self.half_open_since = now
                return True
            return False

        if self.state == "half_open":
            # WP-08 FIX1: stuck trial (client disconnect) gets retried after deadline.
            if self.half_open_in_flight and now - self.half_open_since > self.half_open_deadline_s:
                self.half_open_in_flight = False  # allow a new trial
            if not self.half_open_in_flight:
                self.half_open_in_flight = True
                self.half_open_since = now
                return True
            return False

        return True

    def record(
        self,
        outcome: Outcome,
        now: float | None = None,
        *,
        retry_after_s: float | None = None,
    ) -> None:
        if now is None:
            now = time.monotonic()

        if outcome in (Outcome.LOCAL_OVERLOAD, Outcome.CANCELLED):
            if self.state == "half_open":
                self.half_open_in_flight = False
            return

        if outcome in (Outcome.OK, Outcome.EMPTY):
            self.state = "closed"
            self.consecutive_failures = 0
            self.consecutive_timeouts = 0
            self.cooldown_s = 30.0
            self.retry_at = 0.0
            self.half_open_in_flight = False
            return

        if outcome == Outcome.TIMEOUT:
            self.consecutive_timeouts += 1
            if self.state == "half_open" or self.consecutive_timeouts >= self.timeout_threshold:
                self._trip(now, retry_after_s=retry_after_s)
            return

        if outcome in (Outcome.UPSTREAM_ERROR, Outcome.RATE_LIMITED, Outcome.BLOCKED):
            self.consecutive_failures += 1
            if self.state == "half_open" or self.consecutive_failures >= self.threshold:
                self._trip(now, retry_after_s=retry_after_s)
            return

    def _trip(self, now: float, *, retry_after_s: float | None = None) -> None:
        self.state = "open"
        self.half_open_in_flight = False
        if retry_after_s is not None and retry_after_s > 0:
            self.retry_at = now + retry_after_s
            self.cooldown_s = max(30.0, retry_after_s)
        else:
            jitter = secrets.SystemRandom().uniform(0.8, 1.2)
            dur = min(self.cooldown_s, self.max_cooldown_s) * jitter
            self.retry_at = now + dur
            self.cooldown_s = min(self.cooldown_s * 2, self.max_cooldown_s)


class EngineBreakerRegistry:
    def __init__(self, threshold: int = 3, timeout_threshold: int = 5) -> None:
        self._breakers: dict[str, EngineBreaker] = {}
        self._threshold = threshold
        self._timeout_threshold = timeout_threshold

    def get(self, engine_id: str) -> EngineBreaker:
        if engine_id not in self._breakers:
            self._breakers[engine_id] = EngineBreaker(
                threshold=self._threshold,
                timeout_threshold=self._timeout_threshold,
            )
        return self._breakers[engine_id]

    def allow(self, engine_id: str, now: float | None = None) -> bool:
        return self.get(engine_id).allow(now)

    def record(
        self,
        engine_id: str,
        outcome: Outcome,
        now: float | None = None,
        *,
        retry_after_s: float | None = None,
    ) -> None:
        self.get(engine_id).record(outcome, now, retry_after_s=retry_after_s)

    def filter_candidates(
        self, candidate_ids: list[str], now: float | None = None
    ) -> tuple[list[str], list[str]]:
        if not candidate_ids:
            return [], []

        allowed = []
        skipped = []
        for eid in candidate_ids:
            if self.allow(eid, now):
                allowed.append(eid)
            else:
                skipped.append(eid)

        if not allowed and candidate_ids:
            # FAIL-OPEN: pick the engine with the earliest retry_at
            earliest_id = min(candidate_ids, key=lambda eid: self.get(eid).retry_at)
            allowed = [earliest_id]
            skipped = [eid for eid in candidate_ids if eid != earliest_id]
            self.get(earliest_id).half_open_in_flight = True

        return allowed, skipped


@dataclass
class HealthBucket:
    calls: int = 0
    successes: int = 0
    failures: int = 0
    last_consecutive_failures: int = 0


class EngineHealthWriter:
    """Asynchronous batch background writer for EngineHealth (WP-03).

    Aggregates engine execution results in-memory and flushes once every 10s
    in a single transaction. Search hot path never touches DB.
    """

    def __init__(
        self,
        sessionmaker_provider: Callable[[], Any] | None = None,
        flush_interval_s: float = 10.0,
    ) -> None:
        self._sessionmaker_provider = sessionmaker_provider
        self._flush_interval_s = flush_interval_s
        self._buckets: dict[tuple[str, datetime], HealthBucket] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    def record(
        self,
        engine_id: str,
        *,
        ok: bool,
        latency_ms: int,
        consecutive_failures: int = 0,
    ) -> None:
        window_start = datetime.now(UTC).replace(second=0, microsecond=0)
        bucket = self._buckets.setdefault((engine_id, window_start), HealthBucket())
        bucket.calls += 1
        if ok:
            bucket.successes += 1
        else:
            bucket.failures += 1
        bucket.last_consecutive_failures = consecutive_failures

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self.flush()

    async def _flush_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(self._flush_interval_s)
                await self.flush()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("engine_health_writer_loop_error", error=str(exc))

    async def flush(self) -> None:
        if not self._buckets:
            return
        to_flush = dict(self._buckets)
        self._buckets.clear()

        try:
            from app.db.models import EngineHealth as EngineHealthRow
            from app.orchestration.engine_store import _dialect_insert

            sm = self._sessionmaker_provider() if callable(self._sessionmaker_provider) else None
            if sm is None:
                from app.db.session import get_sessionmaker

                sm = get_sessionmaker()

            async with sm() as session:
                insert = _dialect_insert(session)
                for (engine_id, window_start), bucket in to_flush.items():
                    await session.execute(
                        insert(EngineHealthRow)
                        .values(
                            engine_id=engine_id,
                            window_start=window_start,
                            calls=bucket.calls,
                            successes=bucket.successes,
                            failures=bucket.failures,
                            consecutive_failures=bucket.last_consecutive_failures,
                        )
                        .on_conflict_do_update(
                            index_elements=["engine_id", "window_start"],
                            set_={
                                "calls": EngineHealthRow.calls + bucket.calls,
                                "successes": EngineHealthRow.successes + bucket.successes,
                                "failures": EngineHealthRow.failures + bucket.failures,
                                "consecutive_failures": bucket.last_consecutive_failures,
                            },
                        )
                    )
                await session.commit()
        except Exception as exc:
            logger.warning("engine_health_flush_failed", error=str(exc))
