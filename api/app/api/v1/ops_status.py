"""Operations status — what is up and what is failing (docs/01 persona P4).

Aggregates the readiness probes, engine health and service version into one
payload so an operator (or agent) can ask "cái gì chạy được, cái gì fail?"
in a single call. Always returns 200: a dead dependency is *data*, not an
HTTP error.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app import __version__
from app.api import deps, ops
from app.config import get_settings

router = APIRouter(tags=["ops"])


@router.get("/ops/status")
async def ops_status() -> dict[str, Any]:
    settings = get_settings()
    readiness = await ops.collect_readiness()
    store = deps.get_engine_store()
    try:
        health = await store.health()
        snapshot = await store.snapshot()
    except Exception:  # store fallbacks swallow their own errors, be safe
        health, snapshot = {}, {}

    engines = []
    for engine_id, cfg in snapshot.items():
        summary = health.get(engine_id)
        entry = await deps.get_registry().get(engine_id)
        engines.append(
            {
                "id": engine_id,
                "enabled": cfg.enabled,
                "adapter_ready": bool(entry and entry[1]),
                "disabled_reason": cfg.disabled_reason,
                "calls": summary.calls if summary else 0,
                "failures": summary.failures if summary else 0,
                "consecutive_failures": (summary.consecutive_failures if summary else 0),
            }
        )

    dependencies = {
        name: {"ok": check["ok"], "error": check.get("error")}
        for name, check in readiness["checks"].items()
    }
    failing = [name for name, dep in dependencies.items() if not dep["ok"]]
    return {
        "version": __version__,
        "environment": settings.environment,
        "ready": readiness["ready"],
        "dependencies": dependencies,
        "engines": engines,
        "failing": failing,
    }
