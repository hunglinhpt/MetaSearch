"""Skill discovery & TaskObserver telemetry endpoints (one-skill-to-rule-them-all-inspired)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from app.config import get_settings
from app.core.auth import require_operator
from app.orchestration.skill_orchestrator import get_task_observer, list_skills, select_skill

router = APIRouter(tags=["skills"])


@router.get("/skills", summary="List registered skills and optionally preview query routing")
async def discover_skills(
    q: str | None = Query(default=None, max_length=1000),
    domain: str = Query(default="auto", max_length=60),
    deep: bool = Query(default=False),
) -> dict[str, Any]:
    skills = [
        {
            "id": s.id,
            "domain_id": s.domain_id,
            "name": s.name,
            "description": s.description,
            "compression_profile": s.compression_profile,
            "preferred_engines": s.preferred_engines,
            "verification_rubric": s.verification_rubric,
        }
        for s in list_skills()
    ]
    out: dict[str, Any] = {
        "enabled": bool(get_settings().skill_orchestration_enabled),
        "total": len(skills),
        "skills": skills,
    }
    if q is not None and q.strip():
        sel = select_skill(
            q,
            requested_domain=domain,
            deep=deep,
            min_words_deep=get_settings().adaptive_router_min_words_deep,
        )
        out["routed"] = {
            **sel.to_dict(),
            "prompt_instruction": sel.prompt_instruction,
        }
    return out


@router.get(
    "/ops/skills",
    dependencies=[Depends(require_operator)],
    summary="TaskObserver execution & citation-friction telemetry per skill",
)
async def skill_telemetry() -> dict[str, Any]:
    return {
        "enabled": bool(get_settings().skill_orchestration_enabled),
        **get_task_observer().snapshot(),
    }
