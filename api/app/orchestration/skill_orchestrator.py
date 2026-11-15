"""Unified Skill Orchestrator & Continuous Task Observer (one-skill-to-rule-them-all-inspired).

Routes incoming queries to deterministic domain + complexity skills with concise
progressive prompt disclosure (instead of flooding every turn with verbose multi-domain
rubrics), chooses compression profiles matching domain fidelity requirements, and
records bounded per-skill execution & friction telemetry.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal

from app.retrieval.domain_intelligence import (
    DOMAIN_INTELLIGENCE_REGISTRY,
    classify_domain_intelligence,
)
from app.retrieval.router import classify_complexity

CompressionProfile = Literal["preserve_numbers", "preserve_code", "balanced", "compact"]

_DOMAIN_TO_SKILL: dict[str, str] = {
    "financial": "financial_forensics",
    "biomedical_health": "biomedical_clinical",
    "technology_ai": "technology_engineering",
    "academic_research": "academic_synthesis",
    "legal_macro_policy": "legal_macro_policy",
    "eastern_metaphysics": "eastern_metaphysics",
    "news_geopolitics": "news_geopolitics",
    "general_discovery": "general_grounded_search",
}

_SKILL_PROMPTS: dict[str, str] = {
    "financial_forensics": (
        "Active Skill [financial_forensics]: Apply forensic balance-sheet & cash-flow checks "
        "(CFO/Net Income, CAMELS/Margin/Unbilled Bookings, exact Fibonacci/Ichimoku/RSI). "
        "Cite exact source metrics [n]; never invent price levels or estimates."
    ),
    "biomedical_clinical": (
        "Active Skill [biomedical_clinical]: Apply PICO framing and rank evidence strength "
        "(meta-analysis/RCT > cohort > preclinical). Include caveats and safety disclaimers [n]."
    ),
    "technology_engineering": (
        "Active Skill [technology_engineering]: Focus on versioned APIs, concrete benchmarks, "
        "architecture tradeoffs, and security boundaries grounded strictly in cited docs [n]."
    ),
    "academic_synthesis": (
        "Active Skill [academic_synthesis]: Highlight methodology, dataset size, statistical "
        "effect, and primary peer-reviewed attribution [n]."
    ),
    "legal_macro_policy": (
        "Active Skill [legal_macro_policy]: Anchor every rule to jurisdiction, enacting body, "
        "and effective date with primary statutory/regulatory citations [n]."
    ),
    "eastern_metaphysics": (
        "Active Skill [eastern_metaphysics]: Present classical frameworks accurately alongside "
        "historical context without framing metaphysical claims as empirical measurements [n]."
    ),
    "news_geopolitics": (
        "Active Skill [news_geopolitics]: Organize chronologically with publication dates and "
        "attribute claims to primary outlets [n]; separate facts from statements."
    ),
    "deep_research_synthesis": (
        "Active Skill [deep_research_synthesis]: Decompose into sub-questions, resolve source "
        "disagreements explicitly, and organize structured sections with [n] citations."
    ),
    "general_grounded_search": (
        "Active Skill [general_grounded_search]: Synthesize concisely from highest-trust "
        "sources [n], keeping the response in the user's query language."
    ),
}

_COMPRESSION_PROFILES: dict[str, CompressionProfile] = {
    "financial_forensics": "preserve_numbers",
    "biomedical_clinical": "preserve_numbers",
    "academic_synthesis": "preserve_numbers",
    "technology_engineering": "preserve_code",
    "legal_macro_policy": "balanced",
    "eastern_metaphysics": "balanced",
    "news_geopolitics": "compact",
    "deep_research_synthesis": "balanced",
    "general_grounded_search": "balanced",
}


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    """Registered skill specification returned by discovery endpoints."""

    id: str
    domain_id: str
    name: str
    description: str
    compression_profile: CompressionProfile
    preferred_engines: list[str]
    verification_rubric: list[str]


@dataclass(frozen=True, slots=True)
class SkillSelection:
    """Deterministic routing decision for a single request."""

    skill_id: str
    domain_id: str
    domain_name: str
    complexity: str  # simple | standard | deep
    recommended_mode: str  # answer | deep_research
    compression_profile: CompressionProfile
    preferred_engines: list[str]
    prompt_instruction: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "domain_id": self.domain_id,
            "domain_name": self.domain_name,
            "complexity": self.complexity,
            "recommended_mode": self.recommended_mode,
            "compression_profile": self.compression_profile,
            "preferred_engines": list(self.preferred_engines),
        }


@dataclass(slots=True)
class SkillRunEvent:
    """One recorded execution sample in the bounded TaskObserver ring buffer."""

    ts: float
    skill_id: str
    domain_id: str
    complexity: str
    ok: bool
    latency_ms: int
    compression_saved_pct: float = 0.0
    citations_count: int = 0
    invalid_markers_count: int = 0
    unsupported_markers_count: int = 0
    error_code: str | None = None


@dataclass(slots=True)
class _SkillAggregate:
    runs: int = 0
    successes: int = 0
    failures: int = 0
    friction_events: int = 0
    total_latency_ms: int = 0
    total_saved_pct: float = 0.0
    last_error: str | None = None
    sample_errors: list[str] = field(default_factory=list)


class TaskObserver:
    """Bounded in-memory execution & citation-friction monitor per skill."""

    def __init__(self, max_events: int = 250) -> None:
        self._lock = threading.Lock()
        self._events: deque[SkillRunEvent] = deque(maxlen=max_events)
        self._agg: dict[str, _SkillAggregate] = {}

    def record(
        self,
        *,
        skill_id: str,
        domain_id: str,
        complexity: str,
        ok: bool,
        latency_ms: int,
        compression_saved_pct: float = 0.0,
        citations_count: int = 0,
        invalid_markers_count: int = 0,
        unsupported_markers_count: int = 0,
        error_code: str | None = None,
    ) -> None:
        ev = SkillRunEvent(
            ts=time.time(),
            skill_id=skill_id,
            domain_id=domain_id,
            complexity=complexity,
            ok=ok,
            latency_ms=max(0, int(latency_ms)),
            compression_saved_pct=float(compression_saved_pct),
            citations_count=max(0, int(citations_count)),
            invalid_markers_count=max(0, int(invalid_markers_count)),
            unsupported_markers_count=max(0, int(unsupported_markers_count)),
            error_code=error_code,
        )
        with self._lock:
            self._events.append(ev)
            agg = self._agg.setdefault(skill_id, _SkillAggregate())
            agg.runs += 1
            if ok:
                agg.successes += 1
            else:
                agg.failures += 1
                if error_code:
                    agg.last_error = error_code[:120]
                    if len(agg.sample_errors) < 5:
                        agg.sample_errors.append(agg.last_error)
            if not ok or invalid_markers_count > 0 or unsupported_markers_count > 0:
                agg.friction_events += 1
            agg.total_latency_ms += ev.latency_ms
            agg.total_saved_pct += ev.compression_saved_pct

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            skills_out: dict[str, dict[str, Any]] = {}
            for sid, agg in sorted(self._agg.items()):
                avg_lat = round(agg.total_latency_ms / agg.runs, 1) if agg.runs else 0.0
                avg_saved = round(agg.total_saved_pct / agg.runs, 2) if agg.runs else 0.0
                friction_rate = round(agg.friction_events / agg.runs, 3) if agg.runs else 0.0
                skills_out[sid] = {
                    "runs": agg.runs,
                    "successes": agg.successes,
                    "failures": agg.failures,
                    "friction_events": agg.friction_events,
                    "friction_rate": friction_rate,
                    "avg_latency_ms": avg_lat,
                    "avg_compression_saved_pct": avg_saved,
                    "last_error": agg.last_error,
                }
            recent = [
                {
                    "ts": round(e.ts, 3),
                    "skill_id": e.skill_id,
                    "domain_id": e.domain_id,
                    "complexity": e.complexity,
                    "ok": e.ok,
                    "latency_ms": e.latency_ms,
                    "compression_saved_pct": e.compression_saved_pct,
                    "citations_count": e.citations_count,
                    "invalid_markers_count": e.invalid_markers_count,
                    "unsupported_markers_count": e.unsupported_markers_count,
                    "error_code": e.error_code,
                }
                for e in list(self._events)[-20:]
            ]
            return {
                "total_recorded": sum(a.runs for a in self._agg.values()),
                "skills": skills_out,
                "recent_events": recent,
            }

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._agg.clear()


_observer = TaskObserver()


def get_task_observer() -> TaskObserver:
    return _observer


def list_skills() -> list[SkillDefinition]:
    """Enumerate all registered orchestration skills derived from domain packs + deep synthesis."""
    skills: list[SkillDefinition] = []
    for dom_id, pack in DOMAIN_INTELLIGENCE_REGISTRY.items():
        skill_id = _DOMAIN_TO_SKILL.get(dom_id, f"{dom_id}_skill")
        skills.append(
            SkillDefinition(
                id=skill_id,
                domain_id=dom_id,
                name=pack.name,
                description=pack.description,
                compression_profile=_COMPRESSION_PROFILES.get(skill_id, "balanced"),
                preferred_engines=list(pack.preferred_engines),
                verification_rubric=list(pack.verification_rubric),
            )
        )
    skills.append(
        SkillDefinition(
            id="deep_research_synthesis",
            domain_id="general_discovery",
            name="Deep Multi-Hop Research Synthesis",
            description=(
                "Triggered for multi-hop comparative or long investigative queries "
                "requiring sub-question decomposition."
            ),
            compression_profile="balanced",
            preferred_engines=["searxng", "ddg", "wikipedia", "arxiv", "google_news"],
            verification_rubric=[
                "Did synthesis cover multiple independent viewpoints and cite primary evidence [n]?"
            ],
        )
    )
    return skills


def select_skill(
    query: str,
    *,
    requested_domain: str = "auto",
    deep: bool = False,
    min_words_deep: int = 14,
) -> SkillSelection:
    """Deterministically route ``query`` to the best-fit skill and compression profile."""
    pack = classify_domain_intelligence(query, requested_domain=requested_domain)
    complexity = "deep" if deep else classify_complexity(query, min_words_deep=min_words_deep)
    domain_skill = _DOMAIN_TO_SKILL.get(pack.id, "general_grounded_search")

    # Specialized domains retain their forensic rubric even when complexity is deep;
    # general queries routed as deep use deep_research_synthesis.
    if domain_skill == "general_grounded_search" and complexity == "deep":
        eff_skill = "deep_research_synthesis"
    else:
        eff_skill = domain_skill

    recommended_mode = "deep_research" if complexity == "deep" else "answer"
    instruction = _SKILL_PROMPTS.get(eff_skill, _SKILL_PROMPTS["general_grounded_search"])
    return SkillSelection(
        skill_id=eff_skill,
        domain_id=pack.id,
        domain_name=pack.name,
        complexity=complexity,
        recommended_mode=recommended_mode,
        compression_profile=_COMPRESSION_PROFILES.get(eff_skill, "balanced"),
        preferred_engines=list(pack.preferred_engines),
        prompt_instruction=instruction,
    )
