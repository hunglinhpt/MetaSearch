"""Offload CPU-bound synchronous work to worker threads (docs/14-readiness WP-05)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar

from app.config import get_settings

T = TypeVar("T")

_cpu_sem: asyncio.Semaphore | None = None


def get_cpu_semaphore() -> asyncio.Semaphore:
    global _cpu_sem
    if _cpu_sem is None:
        concurrency = get_settings().extract_concurrency
        _cpu_sem = asyncio.Semaphore(max(1, concurrency))
    return _cpu_sem


def reset_cpu_semaphore() -> None:
    """Reset CPU offload semaphore (for tests)."""
    global _cpu_sem
    _cpu_sem = None


async def run_cpu[T](fn: Callable[..., T], *args: object, **kwargs: object) -> T:
    """Run a CPU-bound function in a worker thread bounded by extract_concurrency semaphore."""
    sem = get_cpu_semaphore()
    async with sem:
        return await asyncio.to_thread(fn, *args, **kwargs)
