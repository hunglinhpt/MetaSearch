"""Admission control and concurrency gating by request tier (docs/14-readiness WP-07)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.core.errors import ErrorCode, MetaSearchError
from app.core.logging import get_logger

logger = get_logger(__name__)

# Concurrency limits and queue wait timeouts (docs/14-readiness/03-work-packages.md WP-07)
# search: 64 concurrent, 1.0s queue wait
# context: 16 concurrent, 2.0s queue wait
# answer: 16 concurrent, 2.0s queue wait
# scrape: 16 concurrent, 1.0s queue wait
# research: max_concurrent (default 5), 0.0s queue wait
_TIER_CONFIG: dict[str, tuple[int, float]] = {
    "search": (64, 1.0),
    "context": (16, 2.0),
    "answer": (16, 2.0),
    "scrape": (16, 1.0),
    "research": (5, 0.0),
}

_semaphores: dict[str, asyncio.Semaphore] = {}
_sem_lock = asyncio.Lock()


async def get_tier_semaphore(tier: str, limit: int) -> asyncio.Semaphore:
    if tier not in _semaphores:
        async with _sem_lock:
            if tier not in _semaphores:
                _semaphores[tier] = asyncio.Semaphore(limit)
    return _semaphores[tier]


def reset_admission() -> None:
    """Reset admission semaphores (for test isolation)."""
    global _semaphores
    _semaphores.clear()


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
    default_limit, default_wait_s = _TIER_CONFIG.get(tier, (16, 1.0))
    limit = default_limit if custom_limit is None else custom_limit
    wait_s = default_wait_s if custom_wait_s is None else custom_wait_s

    sem = await get_tier_semaphore(tier, limit)

    try:
        if wait_s <= 0.0:
            acquired = False
            # If semaphore value > 0, acquire non-blocking
            if sem._value > 0:
                await sem.acquire()
                acquired = True
            if not acquired:
                raise TimeoutError()
        else:
            await asyncio.wait_for(sem.acquire(), timeout=wait_s)
    except TimeoutError:
        retry_after = max(1, int(wait_s + 1))
        logger.warning("admission_overloaded", tier=tier, limit=limit, wait_s=wait_s)
        raise MetaSearchError(
            ErrorCode.OVERLOADED,
            f"Server overloaded for tier '{tier}'; please retry later",
            status_code=503,
            headers={"Retry-After": str(retry_after)},
        ) from None

    try:
        yield
    finally:
        sem.release()
