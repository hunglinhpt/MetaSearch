"""Background worker entrypoint: ``python -m app.worker``.

Phase 2 jobs: heartbeat (engine recovery is managed autonomously by in-memory circuit breakers).
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from app.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine
from app.http import get_http_client

logger = get_logger(__name__)

HEARTBEAT_INTERVAL_S = 30.0


async def _probe_adapter(engine_id: str) -> bool:
    """Cheap liveness check for one engine; True means it works again."""

    from app.api.deps import build_adapters
    from app.retrieval.adapters.searxng import SearXNGAdapter

    adapters = build_adapters(get_settings())
    adapter = adapters.get(engine_id)
    if adapter is None:
        return False
    try:
        if isinstance(adapter, SearXNGAdapter):
            client = get_http_client("internal")
            resp = await client.get(f"{adapter.base_url}/healthz", timeout=5.0)
            return resp.status_code < 500
        results = await adapter.search("metasearch recovery probe", max_results=1, timeout_s=5.0)
        return bool(results)
    except Exception as exc:
        logger.info("engine_probe_failed", engine=engine_id, error=type(exc).__name__)
        return False


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
        stop.set()
        await dispose_engine()
        logger.info("worker_stopped")


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
