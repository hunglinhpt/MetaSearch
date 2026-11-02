"""Engine management endpoints (docs/06-api-spec.md §5, Phase 2 hot config)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api import deps
from app.core.auth import require_admin
from app.core.errors import ErrorCode, MetaSearchError

router = APIRouter(tags=["engines"])


class EnginePatchRequest(BaseModel):
    enabled: bool | None = None
    weight: float | None = Field(default=None, ge=0, le=10)
    timeout_ms: int | None = Field(default=None, ge=100, le=30_000)
    max_results: int | None = Field(default=None, ge=1, le=100)


def _config_payload(
    cfg: Any,
    adapter_ready: bool,
    failures: int,
    breaker_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": cfg.id,
        "name": cfg.name,
        "category": cfg.category,
        "weight": cfg.weight,
        "enabled": cfg.enabled,
        "timeout_ms": cfg.timeout_ms,
        "max_results": cfg.max_results,
        "requires_key": cfg.requires_key,
        "adapter_ready": adapter_ready,
        "disabled_reason": cfg.disabled_reason,
        "consecutive_failures": failures,
        "breaker": breaker_info
        or {
            "state": "closed",
            "retry_at": None,
            "consecutive_failures": failures,
        },
    }


@router.get("/engines")
async def engines() -> list[dict[str, Any]]:
    registry = deps.get_registry()
    store = deps.get_engine_store()
    health_reg = deps.get_health_registry()
    snapshot = await store.snapshot()
    health = await store.health()
    adapters = {cfg.id: adapter for cfg, adapter in await registry.all_entries()}
    items = []
    for cfg in snapshot.values():
        breaker = health_reg.get(cfg.id)
        failures = (
            health[cfg.id].consecutive_failures
            if cfg.id in health
            else breaker.consecutive_failures
        )
        breaker_info = {
            "state": breaker.state,
            "retry_at": breaker.retry_at if breaker.state != "closed" else None,
            "consecutive_failures": breaker.consecutive_failures,
        }
        items.append(
            _config_payload(
                cfg,
                adapter_ready=adapters.get(cfg.id) is not None,
                failures=failures,
                breaker_info=breaker_info,
            )
        )
    return items


@router.get("/engines/health")
async def engines_health() -> list[dict[str, Any]]:
    store = deps.get_engine_store()
    out = []
    for summary in (await store.health()).values():
        success_rate = summary.successes / summary.calls if summary.calls > 0 else None
        out.append(
            {
                "engine": summary.engine_id,
                "enabled": summary.enabled,
                "calls": summary.calls,
                "successes": summary.successes,
                "failures": summary.failures,
                "success_rate": round(success_rate, 4) if success_rate is not None else None,
                "consecutive_failures": summary.consecutive_failures,
                "disabled_reason": summary.disabled_reason,
            }
        )
    return out


@router.patch("/engines/{engine_id}", dependencies=[Depends(require_admin)])
async def patch_engine(engine_id: str, request: EnginePatchRequest) -> dict[str, Any]:
    store = deps.get_engine_store()
    snapshot = await store.snapshot()
    if engine_id not in snapshot:
        # Guard before the upsert: PATCH must not invent new engines.
        raise MetaSearchError(
            ErrorCode.INVALID_REQUEST, f"unknown engine: {engine_id}", status_code=404
        )
    fields = {
        "enabled": request.enabled,
        "weight": request.weight,
        "timeout_ms": request.timeout_ms,
        "max_results": request.max_results,
    }
    updated = await store.update(engine_id, {k: v for k, v in fields.items() if v is not None})
    if updated is None:
        raise MetaSearchError(
            ErrorCode.INVALID_REQUEST, f"unknown engine: {engine_id}", status_code=404
        )
    adapters = {cfg.id: adapter for cfg, adapter in await deps.get_registry().all_entries()}
    health = await store.health()
    summary = health.get(engine_id)
    health_reg = deps.get_health_registry()
    breaker = health_reg.get(engine_id)
    breaker_info = {
        "state": breaker.state,
        "retry_at": breaker.retry_at if breaker.state != "closed" else None,
        "consecutive_failures": breaker.consecutive_failures,
    }
    return _config_payload(
        updated,
        adapter_ready=adapters.get(engine_id) is not None,
        failures=summary.consecutive_failures if summary else 0,
        breaker_info=breaker_info,
    )
