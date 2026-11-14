"""Admission control and concurrency gating by request tier (readiness WP-07)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from app.core.errors import ErrorCode, MetaSearchError
from app.core.logging import get_logger
from app.core.metrics import ADMISSION_REJECTED

logger = get_logger(__name__)

# Concurrency limits and queue wait timeouts (readiness WP-07)
# search: 64 concurrent, 1.0s queue wait
# context: 16 concurrent, 1.0s queue wait
# answer: 16 concurrent, 1.0s queue wait
# (1.0s, not 2.0s: under 100 concurrent callers the rejection itself adds ~0.5s,
# and the plan bounds the whole rejection at 2s. B16 in status/W2-C.md.)
# scrape: 16 concurrent, 1.0s queue wait
# research: max_concurrent (default 5), 0.0s queue wait
# tts: 8 concurrent, 1.0s queue wait (upstream synthesis can take seconds)
# market: 16 concurrent, 1.0s (fa/compare fans out to FA sources; market/ohlcv
# is one feed call) — docs/18 B1/B2
# watchlist: 4 concurrent, 1.0s (alerts fan out one search per item) — docs/18 C
_TIER_CONFIG: dict[str, tuple[int, float]] = {
    "search": (64, 1.0),
    "context": (16, 1.0),
    "answer": (16, 1.0),
    "scrape": (16, 1.0),
    # "research": (5, 0.0) — DEAD (audit round 10): no admit("research") call
    # exists; research concurrency is enforced by the job store's global cap
    # (research_max_concurrent). Restore only with an admit call in run_job.
    "tts": (8, 1.0),
    "market": (16, 1.0),
    "watchlist": (4, 1.0),
    # v3.1: background knowledge-graph builds (docs/24 §8 P3 — off the request path).
    "kg": (4, 1.0),
}

_semaphores: dict[str, asyncio.Semaphore] = {}
_key_semaphores: dict[str, asyncio.Semaphore] = {}
_sem_lock = asyncio.Lock()


async def get_tier_semaphore(tier: str, limit: int) -> asyncio.Semaphore:
    if tier not in _semaphores:
        async with _sem_lock:
            if tier not in _semaphores:
                _semaphores[tier] = asyncio.Semaphore(limit)
    return _semaphores[tier]


async def get_key_semaphore(key_id: str, limit: int) -> asyncio.Semaphore:
    if key_id not in _key_semaphores:
        async with _sem_lock:
            if key_id not in _key_semaphores:
                _key_semaphores[key_id] = asyncio.Semaphore(limit)
    return _key_semaphores[key_id]


def reset_admission() -> None:
    """Reset admission semaphores (for test isolation)."""
    global _semaphores
    _semaphores.clear()
    _key_semaphores.clear()


class AdmissionSlot:
    """Holds acquired concurrency slots (tier and/or per-key) until released."""

    __slots__ = ("_released", "_releases")

    def __init__(self, releases: list[Callable[[], None]]) -> None:
        self._releases = releases
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            for r in reversed(self._releases):
                with contextlib.suppress(Exception):
                    r()


async def acquire_admit_slot(
    tier: str,
    *,
    custom_limit: int | None = None,
    custom_wait_s: float | None = None,
) -> AdmissionSlot:
    """Acquire concurrency permits for tier and calling key (WP-07/WP-08)."""
    releases: list[Callable[[], None]] = []

    try:
        from app.core.auth import current_auth_context

        ctx = current_auth_context.get()
    except Exception:
        ctx = None

    if ctx is not None and ctx.key_id != "anonymous" and ctx.max_concurrent > 0:
        key_sem = await get_key_semaphore(ctx.key_id, ctx.max_concurrent)
        acquired_key = False
        if key_sem._value > 0:
            await key_sem.acquire()
            acquired_key = True
        if not acquired_key:
            ADMISSION_REJECTED.labels(route_class=tier).inc()
            logger.warning(
                "per_key_concurrency_exceeded",
                key_id=ctx.key_id,
                max_concurrent=ctx.max_concurrent,
            )
            raise MetaSearchError(
                ErrorCode.RATE_LIMITED,
                f"Per-key concurrency limit ({ctx.max_concurrent}) exceeded",
                status_code=429,
                headers={"Retry-After": "1"},
            )
        releases.append(key_sem.release)

    default_limit, default_wait_s = _TIER_CONFIG.get(tier, (16, 1.0))
    limit = default_limit if custom_limit is None else custom_limit
    wait_s = default_wait_s if custom_wait_s is None else custom_wait_s

    sem = await get_tier_semaphore(tier, limit)

    try:
        if wait_s <= 0.0:
            acquired = False
            if sem._value > 0:
                await sem.acquire()
                acquired = True
            if not acquired:
                raise TimeoutError()
        else:
            await asyncio.wait_for(sem.acquire(), timeout=wait_s)
        releases.append(sem.release)
    except TimeoutError:
        for r in reversed(releases):
            with contextlib.suppress(Exception):
                r()
        ADMISSION_REJECTED.labels(route_class=tier).inc()
        retry_after = max(1, int(wait_s + 1))
        logger.warning("admission_overloaded", tier=tier, limit=limit, wait_s=wait_s)
        raise MetaSearchError(
            ErrorCode.OVERLOADED,
            f"Server overloaded for tier '{tier}'; please retry later",
            status_code=503,
            headers={"Retry-After": str(retry_after)},
        ) from None
    except BaseException:
        # Includes cancellation while queued: the key permit must not outlive the caller.
        for r in reversed(releases):
            with contextlib.suppress(Exception):
                r()
        raise

    return AdmissionSlot(releases)


@asynccontextmanager
async def admit(
    tier: str,
    *,
    custom_limit: int | None = None,
    custom_wait_s: float | None = None,
) -> AsyncIterator[None]:
    """Gate access by concurrency limit and maximum queue wait timeout.

    When the queue is full or wait timeout expires, immediately raises 503 OVERLOADED
    with Retry-After header.
    """
    slot = await acquire_admit_slot(tier, custom_limit=custom_limit, custom_wait_s=custom_wait_s)
    try:
        yield
    finally:
        slot.release()
