"""PersistQueue (WP-13): bounded queue, single consumer, batched DB writes.

Replaces unbounded fire-and-forget asyncio tasks. A single consumer batches
items in 200ms/100-item windows, preserving FK order (query before answer).
Full when the consumer stalls -> drop oldest + metric.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.core.metrics import BACKGROUND_QUEUE_DEPTH, PERSIST_DROPPED, PERSIST_FAILED

logger = get_logger(__name__)

MAX_SIZE = 1000
BATCH_WINDOW_S = 0.2
BATCH_MAX_ITEMS = 100


@dataclass(slots=True)
class PersistItem:
    label: str
    fn: Callable[..., Any]
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)


class PersistQueue:
    def __init__(self, *, maxsize: int = MAX_SIZE) -> None:
        self._queue: asyncio.Queue[PersistItem | None] = asyncio.Queue(maxsize=maxsize)
        self._consumer_task: asyncio.Task | None = None
        self._dropped = 0
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def dropped(self) -> int:
        return self._dropped

    @staticmethod
    def _discard(item: PersistItem | None) -> None:
        # A dropped write still holds an un-awaited coroutine; close it so nothing leaks.
        if item is None:
            return
        for arg in item.args:
            close = getattr(arg, "close", None)
            if callable(close) and asyncio.iscoroutine(arg):
                close()

    def enqueue(self, item: PersistItem) -> None:
        try:
            self._queue.put_nowait(item)
            BACKGROUND_QUEUE_DEPTH.labels(queue="persist").set(self._queue.qsize())
        except asyncio.QueueFull:
            # Drop oldest to make room (bounded memory under DB stall).
            try:
                self._discard(self._queue.get_nowait())
                self._dropped += 1
                PERSIST_DROPPED.inc()
                BACKGROUND_QUEUE_DEPTH.labels(queue="persist").set(self._queue.qsize())
                logger.warning("persist_queue_dropped_oldest", total_dropped=self._dropped)
                self._queue.put_nowait(item)
            except asyncio.QueueEmpty:
                pass

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._consumer_task = asyncio.create_task(self._consume())

    async def drain(self, budget_s: float = 5.0) -> int:
        """Process all remaining items within budget; return processed count."""
        processed = 0
        deadline = time.monotonic() + budget_s
        while time.monotonic() < deadline:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is None:
                continue
            try:
                await item.fn(*item.args, **item.kwargs)
                processed += 1
            except Exception as exc:
                PERSIST_FAILED.inc()
                logger.warning("persist_drain_failed", label=item.label, error=type(exc).__name__)
        BACKGROUND_QUEUE_DEPTH.labels(queue="persist").set(self._queue.qsize())
        return processed

    async def stop(self) -> None:
        self._running = False
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consumer_task
            self._consumer_task = None

    async def _consume(self) -> None:
        while self._running:
            try:
                first = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            if first is None:
                return
            batch = [first]
            deadline = time.monotonic() + BATCH_WINDOW_S
            while len(batch) < BATCH_MAX_ITEMS and time.monotonic() < deadline:
                try:
                    item = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is None:
                    return
                batch.append(item)
            # The gauge must fall as the consumer drains, not only rise on enqueue.
            BACKGROUND_QUEUE_DEPTH.labels(queue="persist").set(self._queue.qsize())
            for item in batch:
                try:
                    await item.fn(*item.args, **item.kwargs)
                except Exception as exc:
                    PERSIST_FAILED.inc()
                    logger.warning(
                        "persist_batch_item_failed", label=item.label, error=type(exc).__name__
                    )


_queue: PersistQueue | None = None


def get_persist_queue() -> PersistQueue:
    global _queue
    if _queue is None:
        _queue = PersistQueue()
    return _queue


async def reset_persist_queue() -> None:
    global _queue
    if _queue is not None:
        await _queue.stop()
    _queue = None


def discard_persist_queue() -> None:
    """Sync-safe: drop the singleton without awaiting stop (test cleanup)."""
    global _queue
    if _queue is not None and _queue._consumer_task is not None:
        _queue._consumer_task.cancel()
    _queue = None
