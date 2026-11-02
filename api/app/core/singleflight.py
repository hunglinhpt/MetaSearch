"""In-process request coalescing (single-flight) to prevent cache stampedes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


class SingleFlight:
    """Coalesces concurrent invocations for the same key into a single execution."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._waiters: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def run(self, key: str, coro_fn: Callable[[], Coroutine[Any, Any, T]]) -> T:
        async with self._lock:
            if key in self._tasks:
                self._waiters[key] += 1
                task = self._tasks[key]
            else:
                self._waiters[key] = 1
                task = asyncio.create_task(coro_fn())
                self._tasks[key] = task

        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            async with self._lock:
                if key in self._waiters:
                    self._waiters[key] -= 1
                    if self._waiters[key] <= 0:
                        if not task.done():
                            task.cancel()
                        self._tasks.pop(key, None)
                        self._waiters.pop(key, None)
            raise
        finally:
            async with self._lock:
                if task.done() and self._tasks.get(key) is task:
                    self._tasks.pop(key, None)
                    self._waiters.pop(key, None)
