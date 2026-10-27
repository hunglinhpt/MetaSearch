"""Background worker entrypoint: ``python -m app.worker``.

Phase 2 jobs: heartbeat + engine recovery probe (re-enables engines that were
auto-disabled after consecutive failures, docs/12-roadmap-plan.md Phase 2).
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


async def _probe_adapter(engine_id: str) -> bool:
    """Cheap liveness check for one engine; True means it works again."""
    import httpx

    from app.api.deps import build_adapters
    from app.retrieval.adapters.searxng import SearXNGAdapter

    adapters = build_adapters(get_settings())
    adapter = adapters.get(engine_id)
    if adapter is None:
        return False
    try:
        if isinstance(adapter, SearXNGAdapter):
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{adapter.base_url}/healthz")
            return resp.status_code < 500
        results = await adapter.search("metasearch recovery probe", max_results=1, timeout_s=5.0)
        return bool(results)
    except Exception as exc:
        logger.info("engine_probe_failed", engine=engine_id, error=type(exc).__name__)
        return False


async def _recovery_loop(stop: asyncio.Event) -> None:
    settings = get_settings()
    from app.api.deps import get_engine_store, reset_pipelines

    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.engine_recovery_probe_s)
        if stop.is_set():
            return
        reset_pipelines()
        store = get_engine_store()
        try:
            candidates = await store.auto_disabled()
        except Exception as exc:
            logger.warning("recovery_probe_skipped", error=type(exc).__name__)
            continue
        for engine_id in candidates:
            ok = await _probe_adapter(engine_id)
            logger.info("engine_recovery_probe", engine=engine_id, recovered=ok)
            if ok:
                await store.update(engine_id, {"enabled": True})


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

    recovery = asyncio.create_task(_recovery_loop(stop))
    try:
        await _heartbeat(stop)
    finally:
        stop.set()
        recovery.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await recovery
        await dispose_engine()
        logger.info("worker_stopped")


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
