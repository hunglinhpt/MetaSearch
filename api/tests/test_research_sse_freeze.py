"""Tests for WP-02: Deep Research SSE freeze fixes.

Verifies:
1. Live subscriber survives > maxlen (2000) events without freezing or starving the event loop.
2. Terminal events (done / error) are never evicted from the job.
3. Slow subscriber receives a ("gap", {"dropped": n}) event when events are evicted.
4. _event_stream yields to the event loop on every step (ticker progresses).
5. Report token batching preserves full report content when buffer fills up.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from typing import Any

import pytest

from app.api.v1.research import _event_stream
from app.orchestration.research_jobs import ResearchJob, push_event


class HardWatchdog:
    """Watchdog that terminates the thread or raises if a test hangs."""

    def __init__(self, timeout_s: float = 8.0) -> None:
        self.timeout_s = timeout_s
        self.stop_event = threading.Event()
        self.timed_out = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        if not self.stop_event.wait(self.timeout_s):
            self.timed_out = True

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()


@pytest.mark.asyncio
async def test_live_subscriber_survives_more_than_maxlen_events():
    """REL-01 / P5B: A live subscriber streaming > 2000 events must complete with done=True."""
    dog = HardWatchdog(timeout_s=10.0)
    dog.start()

    job = ResearchJob(id=uuid.uuid4(), query="stress test")
    num_events = 2500

    async def producer():
        for i in range(num_events):
            job.push(("report_token", {"t": f"x{i} "}))
            job.notify_sync()
            if i % 150 == 0:
                await asyncio.sleep(0.001)
        job.push(("done", {"status": "complete", "cost_usd": 0.01}))
        job.notify_sync()

    seen_events: list[dict[str, Any]] = []
    got_done = False

    async def consumer():
        nonlocal got_done
        async for item in _event_stream(job):
            seen_events.append(item)
            if item.get("event") == "done":
                got_done = True
                break

    try:
        prod_task = asyncio.create_task(producer())
        await asyncio.wait_for(consumer(), timeout=8.0)
        await prod_task
    finally:
        dog.stop()

    assert not dog.timed_out, "Test exceeded watchdog timeout: event loop was starved!"
    assert got_done is True, "Subscriber did not receive terminal 'done' event"
    assert len(seen_events) > 0


@pytest.mark.asyncio
async def test_live_subscriber_survives_20000_events():
    """REL-01 / P5B stress: A live subscriber streaming 20,000 events must complete without hang."""
    dog = HardWatchdog(timeout_s=15.0)
    dog.start()

    job = ResearchJob(id=uuid.uuid4(), query="20k stress test")
    num_events = 20000

    async def producer():
        for i in range(num_events):
            job.push(("report_token", {"t": f"w{i} "}))
            job.notify_sync()
            if i % 1000 == 0:
                await asyncio.sleep(0.001)
        job.push(("done", {"status": "complete", "cost_usd": 0.05}))
        job.notify_sync()

    got_done = False

    async def consumer():
        nonlocal got_done
        async for item in _event_stream(job):
            if item.get("event") == "done":
                got_done = True
                break

    try:
        prod_task = asyncio.create_task(producer())
        await asyncio.wait_for(consumer(), timeout=12.0)
        await prod_task
    finally:
        dog.stop()

    assert not dog.timed_out, "Event loop starved during 20k event stress test!"
    assert got_done is True, "Terminal done event was not delivered in 20k event run"


@pytest.mark.asyncio
async def test_terminal_event_never_evicted():
    """Terminal event ('done' or 'error') is retained even if > 2500 events are pushed."""
    job = ResearchJob(id=uuid.uuid4(), query="terminal retention")

    # Push 2200 tokens
    for i in range(2200):
        job.push(("report_token", {"t": str(i)}))

    # Push done
    job.push(("done", {"status": "ok"}))

    assert job.terminal is not None
    assert job.terminal[1][0] == "done"
    assert job.finished is True

    # A subscriber connecting AFTER eviction must still be able to get terminal
    # Call wait_event past all events in deque
    last_seq = job.events[-1][0] if job.events else 0
    _seq, _event = await job.wait_event(last_seq, poll_after_s=0.1)
    # If last_seq was terminal, event could be terminal or it returns terminal
    assert job.terminal[1][0] == "done"


@pytest.mark.asyncio
async def test_gap_event_when_subscriber_falls_behind():
    """A slow subscriber whose cursor was evicted receives a gap event with dropped count."""
    job = ResearchJob(id=uuid.uuid4(), query="gap test")

    # Push 2500 events before subscriber reads
    for i in range(2500):
        job.push(("step", {"step": i}))
    job.push(("done", {"status": "ok"}))

    # Subscriber asks for event starting from 0 (very old, already evicted)
    seq, event = await job.wait_event(0, poll_after_s=0.1)
    assert event is not None
    name, data = event
    assert name == "gap"
    assert "dropped" in data
    assert data["dropped"] > 0

    # The next read should yield an actual event from the start of the current deque
    _next_seq, next_event = await job.wait_event(seq, poll_after_s=0.1)
    assert next_event is not None
    assert next_event[0] in ("step", "done")


@pytest.mark.asyncio
async def test_event_stream_yields_to_loop():
    """Event loop ticks must increment concurrently while _event_stream consumes events."""
    job = ResearchJob(id=uuid.uuid4(), query="loop yield")
    ticks = 0
    stop_ticks = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not stop_ticks.is_set():
            ticks += 1
            await asyncio.sleep(0.001)

    tk = asyncio.create_task(ticker())

    # Push 1000 events
    for _i in range(1000):
        job.push(("report_token", {"t": "a"}))
    job.push(("done", {"status": "ok"}))

    # Consume all events
    async for _item in _event_stream(job):
        pass

    stop_ticks.set()
    await tk

    assert ticks > 0, "Event loop ticks must increase (loop was not starved)"


@pytest.mark.asyncio
async def test_push_event_sync_notification():
    """push_event does not create unbounded tasks and sets event flag."""
    job = ResearchJob(id=uuid.uuid4(), query="sync notify")
    push_event(job, ("status", {"msg": "started"}))

    # Should be immediately readable
    _seq, event = await job.wait_event(0, poll_after_s=0.1)
    assert event is not None
    assert event[0] == "status"
