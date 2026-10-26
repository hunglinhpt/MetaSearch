"""Background worker entrypoint: ``python -m app.worker``.

Phase 0 keeps the loop intentionally small (heartbeat + graceful shutdown);
Phase 1+ registers real jobs such as engine health probes and cache warming.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from app.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine

logger = get_logger(__name__)

HEARTBEAT_INTERVAL_S = 30.0


async def _heartbeat(stop: asyncio.Event) -> None:
    while not stop.is_set():
        logger.info("worker_heartbeat")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_S)


async def run() -> None:
    settings = get_settings()
    configure_logging()
    logger.info("worker_starting", environment=settings.environment)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows' ProactorEventLoop has no signal handlers.
            signal.signal(sig, lambda *_: stop.set())

    try:
        await _heartbeat(stop)
    finally:
        await dispose_engine()
        logger.info("worker_stopped")


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
