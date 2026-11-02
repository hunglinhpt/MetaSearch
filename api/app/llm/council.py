"""Multi-Endpoint LLM Council of Judges, Anonymous Cross-Review, and Dream-RSI
(Recursive Self-Improvement via Replay World) Engine.

Architecture:
1. **Dream-RSI Policy & Replay World (``DreamRSIPolicyStore``)**:
   - Records every multi-endpoint execution trajectory (citation precision, peer-review
     scores, quantitative depth, latency, success rate) per domain and endpoint.
   - Runs off-policy "dreaming" replay over historical trajectories to evolve endpoint
     selection weights (UCB1 exploration-exploitation) and temperature policies.
2. **Parallel Multi-Endpoint Generation**:
   - Queries multiple heterogeneous Free LLM endpoints concurrently (e.g., OpenRouter Free,
     Pollinations Keyless, Gemini Free, Groq LPU, NVIDIA NIM, Cerebras, SambaNova).
3. **Anonymous Cross-Review (Blind Peer Jury + Deterministic Forensic Audit)**:
   - Anonymizes drafts as ``Candidate A``, ``Candidate B``, ``Candidate C``...
   - Evaluates each draft against the domain's ``verification_rubric`` (e.g. BCTC Forensics
     & Fibonacci/Ichimoku rigor for Finance, PICO for Biomedical, Complexity/Security for Tech).
4. **Chairman Verdict Synthesis**:
   - Synthesizes the final authoritative answer from the top-ranked candidates and peer
     critiques, then strips any invalid citation markers via ``validate_citations``.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.core.logging import get_logger
from app.extraction.chunker import Chunk
from app.llm.citations import validate_citations
from app.llm.gateway import LLMError, LLMGateway
from app.llm.prompts import SYSTEM_PROMPT, render_context
from app.llm.providers import LLMOverride, get_runtime_llm_store, parse_council_endpoint_specs
from app.retrieval.domain_intelligence import (
    DomainIntelligencePack,
    classify_domain_intelligence,
)

logger = get_logger(__name__)

_QUANT_TOKEN_RE = re.compile(r"\b\d+(?:[.,]\d+)?(?:\s*%|\s*x|\s*tỷ|\s*triệu|\s*bps|\b)", re.I)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Robustly extract a JSON object from text, handling markdown code fences,
    preceding/trailing prose, and nested braces.
    """
    clean_text = text.strip()
    if not clean_text:
        return None

    # 1. Direct parse attempt
    if clean_text.startswith("{") and clean_text.endswith("}"):
        try:
            val = json.loads(clean_text)
            if isinstance(val, dict):
                return val
        except (json.JSONDecodeError, ValueError, TypeError):  # nosec B110
            pass

    # 2. Markdown fence extraction ```json ... ``` or ``` ... ```
    fence_pattern = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
    for m in fence_pattern.finditer(clean_text):
        try:
            val = json.loads(m.group(1))
            if isinstance(val, dict):
                return val
        except (json.JSONDecodeError, ValueError, TypeError):  # nosec B110
            pass

    # 3. Balanced braces extraction
    start_idx = clean_text.find("{")
    while start_idx != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start_idx, len(clean_text)):
            c = clean_text[i]
            if escape:
                escape = False
                continue
            if c == "\\":
                escape = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if not in_string:
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = clean_text[start_idx : i + 1]
                        try:
                            val = json.loads(candidate)
                            if isinstance(val, dict):
                                return val
                        except (json.JSONDecodeError, ValueError, TypeError):  # nosec B110
                            pass
                        break
        start_idx = clean_text.find("{", start_idx + 1)

    return None


@dataclass(slots=True)
class EndpointStats:
    endpoint_key: str  # "provider:model"
    provider: str
    model: str
    trials: int = 0
    successes: int = 0
    avg_citation_precision: float = 0.85
    avg_peer_score: float = 7.5
    avg_latency_ms: float = 1200.0
    last_reward: float = 0.8

    def ucb_score(self, total_trials: int, exploration_c: float = 0.35) -> float:
        """Compute Upper Confidence Bound score for Dream-RSI endpoint selection."""
        if self.trials == 0:
            return 1.25  # Encourage initial exploration of untried free endpoints
        success_rate = self.successes / max(self.trials, 1)
        quality = 0.45 * (self.avg_peer_score / 10.0) + 0.35 * self.avg_citation_precision
        speed_bonus = max(0.0, min(0.20, 0.20 * (3000.0 / max(self.avg_latency_ms, 300.0))))
        mean_reward = (quality + speed_bonus) * success_rate
        explore_term = exploration_c * math.sqrt(
            math.log(max(total_trials, 1) + 1.0) / (self.trials + 1.0)
        )
        return round(mean_reward + explore_term, 4)


@dataclass(slots=True)
class ReplayTrajectory:
    timestamp: float
    domain: str
    query_preview: str
    winner_endpoint: str
    consensus_agreement: float
    best_composite_score: float
    panel_scores: dict[str, float]


@dataclass(slots=True)
class DomainEvolutionPolicy:
    domain: str
    generation: int = 0
    optimal_temperature: float = 0.2
    recommended_panel: list[str] = field(default_factory=list)
    replay_improvement_delta: float = 0.0
    total_replays: int = 0


class DreamRSIPolicyStore:
    """In-memory Replay World & Recursive Self-Improvement policy store."""

    def __init__(self) -> None:
        self._stats: dict[str, EndpointStats] = {}
        self._trajectories: deque[ReplayTrajectory] = deque(maxlen=500)
        self._domain_policies: dict[str, DomainEvolutionPolicy] = {}

    def record_endpoint_outcome(
        self,
        *,
        provider: str,
        model: str,
        ok: bool,
        citation_precision: float,
        peer_score: float,
        latency_ms: int,
    ) -> None:
        key = f"{provider}:{model}"
        st = self._stats.get(key)
        if st is None:
            st = EndpointStats(endpoint_key=key, provider=provider, model=model)
            self._stats[key] = st
        st.trials += 1
        if ok:
            st.successes += 1
            alpha = 0.3
            st.avg_citation_precision = (
                1 - alpha
            ) * st.avg_citation_precision + alpha * citation_precision
            st.avg_peer_score = (1 - alpha) * st.avg_peer_score + alpha * peer_score
            st.avg_latency_ms = (1 - alpha) * st.avg_latency_ms + alpha * float(latency_ms)
            st.last_reward = round(0.5 * (peer_score / 10.0) + 0.5 * citation_precision, 4)
        else:
            st.last_reward = 0.0

    def dream_replay_and_optimize(
        self,
        domain: str,
        query: str,
        panel_scores: dict[str, float],
        consensus_agreement: float,
    ) -> DomainEvolutionPolicy:
        """Execute a Dream-RSI offline replay step over accumulated trajectories
        for ``domain``, evolving the recommended panel and temperature policy.
        """
        winner = (
            max(panel_scores.items(), key=lambda kv: kv[1])[0]
            if panel_scores
            else "openrouter:openrouter/free"
        )
        best_score = max(panel_scores.values()) if panel_scores else 0.0
        self._trajectories.append(
            ReplayTrajectory(
                timestamp=time.time(),
                domain=domain,
                query_preview=query[:120],
                winner_endpoint=winner,
                consensus_agreement=round(consensus_agreement, 4),
                best_composite_score=round(best_score, 4),
                panel_scores=dict(panel_scores),
            )
        )

        policy = self._domain_policies.get(domain)
        if policy is None:
            policy = DomainEvolutionPolicy(domain=domain)
            self._domain_policies[domain] = policy

        domain_runs = [t for t in self._trajectories if t.domain == domain]
        policy.generation += 1
        policy.total_replays = len(domain_runs)

        # Replay World simulation: aggregate historical counterfactual performance per endpoint
        agg_scores: dict[str, list[float]] = {}
        for traj in domain_runs:
            for ep_key, sc in traj.panel_scores.items():
                agg_scores.setdefault(ep_key, []).append(sc)

        total_trials = sum(s.trials for s in self._stats.values())
        ranked_eps = sorted(
            agg_scores.keys(),
            key=lambda k: (
                (sum(agg_scores[k]) / len(agg_scores[k])) * 0.7
                + (self._stats[k].ucb_score(total_trials) * 3.0 if k in self._stats else 2.0)
            ),
            reverse=True,
        )
        policy.recommended_panel = ranked_eps[:4]

        # Adapt exploration temperature based on consensus agreement in replay memory
        avg_agreement = (
            sum(t.consensus_agreement for t in domain_runs) / len(domain_runs)
            if domain_runs
            else 0.75
        )
        if avg_agreement < 0.6:
            policy.optimal_temperature = 0.15  # Tighten determinism when disagreement is high
        elif avg_agreement > 0.85:
            policy.optimal_temperature = 0.25  # Allow slightly broader exploration
        else:
            policy.optimal_temperature = 0.20

        if len(domain_runs) >= 2:
            first_score = domain_runs[0].best_composite_score
            latest_score = domain_runs[-1].best_composite_score
            policy.replay_improvement_delta = round(latest_score - first_score, 4)

        return policy

    def rank_overrides(
        self, candidates: list[LLMOverride], settings: Settings
    ) -> list[LLMOverride]:
        """Order candidate endpoints by Dream-RSI UCB score."""
        total_trials = sum(s.trials for s in self._stats.values())

        def _score(ov: LLMOverride) -> float:
            prov = ov.provider or settings.llm_provider
            mod = ov.model or ov.quick_model or "default"
            key = f"{prov}:{mod}"
            st = self._stats.get(key)
            return st.ucb_score(total_trials) if st else 1.25

        return sorted(candidates, key=_score, reverse=True)

    def snapshot(self, domain: str | None = None) -> dict[str, Any]:
        total_trials = sum(s.trials for s in self._stats.values())
        endpoints_list = [
            {
                "endpoint": s.endpoint_key,
                "provider": s.provider,
                "model": s.model,
                "trials": s.trials,
                "successes": s.successes,
                "avg_citation_precision": round(s.avg_citation_precision, 4),
                "avg_peer_score": round(s.avg_peer_score, 2),
                "avg_latency_ms": round(s.avg_latency_ms, 1),
                "ucb_score": s.ucb_score(total_trials),
            }
            for s in sorted(
                self._stats.values(),
                key=lambda item: item.ucb_score(total_trials),
                reverse=True,
            )
        ]
        if domain and domain in self._domain_policies:
            gen = self._domain_policies[domain].generation
        elif self._domain_policies:
            gen = max(p.generation for p in self._domain_policies.values())
        else:
            gen = 0
        return {
            "generation": gen,
            "replay_buffer_size": len(self._trajectories),
            "total_trajectories": len(self._trajectories),
            "total_endpoint_trials": total_trials,
            "endpoint_ranking": endpoints_list,
            "endpoints": endpoints_list,
            "domain_policies": {
                dom: {
                    "generation": pol.generation,
                    "optimal_temperature": pol.optimal_temperature,
                    "recommended_panel": pol.recommended_panel,
                    "replay_improvement_delta": pol.replay_improvement_delta,
                    "total_replays": pol.total_replays,
                }
                for dom, pol in self._domain_policies.items()
            },
            "recent_trajectories": [
                {
                    "domain": t.domain,
                    "query_preview": t.query_preview,
                    "winner_endpoint": t.winner_endpoint,
                    "consensus_agreement": t.consensus_agreement,
                    "best_composite_score": t.best_composite_score,
                }
                for t in list(self._trajectories)[-10:]
            ],
        }

    def reset(self) -> None:
        self._stats.clear()
        self._trajectories.clear()
        self._domain_policies.clear()


_dream_rsi_store = DreamRSIPolicyStore()


def get_dream_rsi_store() -> DreamRSIPolicyStore:
    return _dream_rsi_store


@dataclass(slots=True)
class PanelistDraft:
    candidate_id: str  # "Candidate A", "Candidate B", ...
    provider: str
    model: str
    answer_md: str
    valid_citations: list[int]
    invalid_citations: list[int]
    citation_precision: float
    quantitative_density: float
    source_coverage: float
    deterministic_score: float
    peer_score: float
    composite_score: float
    latency_ms: int
    status: str = "ok"
    error: str | None = None
    peer_critique_summary: str = ""


class DreamRSICouncilEngine:
    """Executes Multi-Endpoint LLM Council generation, Anonymous Cross-Review,
    Chairman Verdict synthesis, and Dream-RSI replay optimization.
    """

    def __init__(self, settings: Settings, gateway: LLMGateway) -> None:
        self.settings = settings
        self.gateway = gateway
        self.dream_store = get_dream_rsi_store()

    def select_panel(
        self,
        explicit_endpoints: list[LLMOverride] | None = None,
    ) -> list[LLMOverride]:
        runtime_eps = get_runtime_llm_store().get_council_endpoints()
        raw = explicit_endpoints or runtime_eps or self.settings.llm_council_endpoints
        panel = parse_council_endpoint_specs(raw, self.settings)
        if self.settings.llm_dream_rsi_enabled:
            panel = self.dream_store.rank_overrides(panel, self.settings)
        return panel[:5]

    async def run_council(
        self,
        question: str,
        included: list[tuple[int, Chunk]],
        *,
        endpoints: list[LLMOverride] | None = None,
        domain: str = "auto",
        enable_cross_review: bool = True,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        pack = classify_domain_intelligence(question, requested_domain=domain)
        panel = self.select_panel(endpoints)
        total_sources = max(len(included), 1)
        context_text = render_context(included)

        domain_sys_prompt = (
            f"{SYSTEM_PROMPT}\n\n"
            f"Active Domain Intelligence Pack: {pack.name} ({pack.id}).\n"
            "Focus on deep structural essence across these dimensions:\n"
            + "\n".join(f"- {dim}" for dim in pack.essence_dimensions)
        )
        messages = [
            {"role": "system", "content": domain_sys_prompt},
            {"role": "user", "content": f"Sources:\n\n{context_text}\n\nQuestion: {question}"},
        ]

        # Stage 1: Concurrent Independent Generation across Free LLM Panelists
        drafts = await self._generate_panel_drafts(panel, messages, included, total_sources)
        ok_drafts = [d for d in drafts if d.status == "ok" and d.answer_md.strip()]

        if not ok_drafts:
            errors = [f"{d.provider}:{d.model} -> {d.error}" for d in drafts]
            raise LLMError("All Council LLM endpoints failed: " + "; ".join(errors[:4]))

        # Stage 2: Anonymous Cross-Review (Peer Critique + Deterministic Forensic Audit)
        cross_reviews: list[dict[str, Any]] = []
        if enable_cross_review and len(ok_drafts) >= 2:
            cross_reviews = await self._run_anonymous_cross_review(
                question, context_text, ok_drafts, panel, pack
            )
        else:
            for d in ok_drafts:
                d.peer_score = d.deterministic_score
                d.composite_score = d.deterministic_score
                d.peer_critique_summary = (
                    "Validated by deterministic citation & quantitative auditor."
                )

        ok_drafts.sort(key=lambda d: -d.composite_score)
        consensus_agreement = _compute_consensus_agreement(ok_drafts)

        # Stage 3: Council Chairman Verdict Synthesis
        winner = ok_drafts[0]
        if len(ok_drafts) >= 2:
            verdict_raw, chairman_label = await self._synthesize_chairman_verdict(
                question, context_text, ok_drafts, panel[0], winner, pack
            )
        else:
            verdict_raw = winner.answer_md
            chairman_label = f"{winner.provider}:{winner.model}"

        check = validate_citations(verdict_raw, included)

        # Stage 4: Update Dream-RSI Replay World & Evolve Exploration Policy
        panel_score_map: dict[str, float] = {}
        for d in drafts:
            self.dream_store.record_endpoint_outcome(
                provider=d.provider,
                model=d.model,
                ok=(d.status == "ok"),
                citation_precision=d.citation_precision,
                peer_score=d.composite_score,
                latency_ms=d.latency_ms,
            )
            if d.status == "ok":
                panel_score_map[f"{d.provider}:{d.model}"] = d.composite_score

        policy = self.dream_store.dream_replay_and_optimize(
            domain=pack.id,
            query=question,
            panel_scores=panel_score_map,
            consensus_agreement=consensus_agreement,
        )

        latency_ms = int((time.perf_counter() - started) * 1000)
        return {
            "domain": pack.id,
            "domain_name": pack.name,
            "domain_maturity": pack.maturity,
            "answer_md": check.text,
            "verdict_md": check.text,
            "citations": [
                {"n": c.n, "url": c.url, "title": c.title, "chunk_id": c.chunk_id}
                for c in check.citations
            ],
            "invalid_markers": check.invalid_markers,
            "chairman_model": chairman_label,
            "consensus_agreement_score": round(consensus_agreement, 4),
            "panelists": [
                {
                    "candidate_id": d.candidate_id,
                    "provider": d.provider,
                    "model": d.model,
                    "ok": d.status == "ok",
                    "status": d.status,
                    "answer_md": d.answer_md,
                    "valid_citations": d.valid_citations,
                    "invalid_citations": d.invalid_citations,
                    "citation_precision": round(d.citation_precision, 4),
                    "quantitative_density": round(d.quantitative_density, 4),
                    "source_coverage": round(d.source_coverage, 4),
                    "deterministic_score": round(d.deterministic_score, 2),
                    "peer_score": round(d.peer_score, 2),
                    "composite_score": round(d.composite_score, 2),
                    "peer_critique_summary": d.peer_critique_summary,
                    "latency_ms": d.latency_ms,
                    "error": d.error,
                }
                for d in drafts
            ],
            "cross_reviews": cross_reviews,
            "dream_rsi_policy": {
                "domain": policy.domain,
                "generation": policy.generation,
                "optimal_temperature": policy.optimal_temperature,
                "recommended_panel": policy.recommended_panel,
                "replay_improvement_delta": policy.replay_improvement_delta,
                "total_replays": policy.total_replays,
            },
            "total_latency_ms": latency_ms,
            "latency_ms": latency_ms,
        }

    async def _generate_panel_drafts(
        self,
        panel: list[LLMOverride],
        messages: list[dict[str, str]],
        included: list[tuple[int, Chunk]],
        total_sources: int,
    ) -> list[PanelistDraft]:
        async def _call_one(idx: int, ov: LLMOverride) -> PanelistDraft:
            cand_id = f"Candidate {chr(ord('A') + idx)}"
            t0 = time.perf_counter()
            gw = (
                self.gateway.with_override(ov)
                if hasattr(self.gateway, "with_override")
                else self.gateway
            )
            prov = ov.provider or getattr(gw, "provider", self.settings.llm_provider)
            mod = (
                ov.model
                or ov.quick_model
                or getattr(gw, "default_quick_model", self.settings.llm_quick_model)
            )
            try:
                parts: list[str] = []
                async for delta in gw.stream(messages, model=mod):
                    if delta.text:
                        parts.append(delta.text)
                raw_text = "".join(parts).strip()
                ms = int((time.perf_counter() - t0) * 1000)
                if not raw_text:
                    raise LLMError("empty response")

                check = validate_citations(raw_text, included)
                valid_ns = [c.n for c in check.citations]
                invalid_ns = check.invalid_markers
                total_markers = len(valid_ns) + len(invalid_ns)
                cit_prec = len(valid_ns) / total_markers if total_markers > 0 else 0.5
                src_cov = min(1.0, len(valid_ns) / max(min(total_sources, 4), 1))
                quant_matches = len(_QUANT_TOKEN_RE.findall(raw_text))
                quant_density = min(1.0, quant_matches / 6.0)
                det_score = round(
                    10.0 * (0.45 * cit_prec + 0.35 * src_cov + 0.20 * quant_density), 2
                )
                return PanelistDraft(
                    candidate_id=cand_id,
                    provider=str(prov),
                    model=str(mod),
                    answer_md=check.text,
                    valid_citations=valid_ns,
                    invalid_citations=invalid_ns,
                    citation_precision=cit_prec,
                    quantitative_density=quant_density,
                    source_coverage=src_cov,
                    deterministic_score=det_score,
                    peer_score=det_score,
                    composite_score=det_score,
                    latency_ms=ms,
                    status="ok",
                )
            except Exception as exc:
                ms = int((time.perf_counter() - t0) * 1000)
                return PanelistDraft(
                    candidate_id=cand_id,
                    provider=str(prov),
                    model=str(mod),
                    answer_md="",
                    valid_citations=[],
                    invalid_citations=[],
                    citation_precision=0.0,
                    quantitative_density=0.0,
                    source_coverage=0.0,
                    deterministic_score=0.0,
                    peer_score=0.0,
                    composite_score=0.0,
                    latency_ms=ms,
                    status="failed",
                    error=str(exc)[:200],
                )

        async def _call_with_timeout(idx: int, ov: LLMOverride) -> PanelistDraft:
            cand_id = f"Candidate {chr(ord('A') + idx)}"
            prov = ov.provider or self.settings.llm_provider
            mod = ov.model or ov.quick_model or self.settings.llm_quick_model
            try:
                return await asyncio.wait_for(_call_one(idx, ov), timeout=40.0)
            except TimeoutError:
                return PanelistDraft(
                    candidate_id=cand_id,
                    provider=str(prov),
                    model=str(mod),
                    answer_md="",
                    valid_citations=[],
                    invalid_citations=[],
                    citation_precision=0.0,
                    quantitative_density=0.0,
                    source_coverage=0.0,
                    deterministic_score=0.0,
                    peer_score=0.0,
                    composite_score=0.0,
                    latency_ms=40000,
                    status="failed",
                    error="Endpoint call timed out after 40s",
                )

        return await self._collect_panel(panel, _call_with_timeout)

    async def _collect_panel(self, panel, call_with_timeout) -> list:
        """WP-08: overall wall-clock budget (``council_budget_s``) with partial
        results kept — a slow or failed panelist never blocks the council.
        Provider breakers are enforced inside ``gateway.stream`` itself."""
        budget_s = max(getattr(self.settings, "council_budget_s", 90.0), 10.0)
        tasks = [asyncio.create_task(call_with_timeout(i, ov)) for i, ov in enumerate(panel)]
        done, pending = await asyncio.wait(tasks, timeout=budget_s)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            logger.warning("council_budget_exceeded", budget_s=budget_s, pending=len(pending))

        drafts = []
        for task in tasks:
            if task in done and not task.cancelled():
                exc = task.exception()
                if exc is None:
                    result = task.result()
                    if result is not None:  # a late cancel can resolve to None
                        drafts.append(result)
                else:  # unexpected crash in bookkeeping — degrade, don't block
                    logger.warning("council_panelist_crashed", error=type(exc).__name__)
        drafts.sort(key=lambda d: d.candidate_id)
        return drafts

    async def _run_anonymous_cross_review(
        self,
        question: str,
        context_text: str,
        ok_drafts: list[PanelistDraft],
        panel: list[LLMOverride],
        pack: DomainIntelligencePack,
    ) -> list[dict[str, Any]]:
        """Conduct blind anonymous cross-review where candidates are labeled only as
        Candidate A, Candidate B, etc.
        """
        anonymized_block = "\n\n".join(
            f"=== {d.candidate_id} ===\n{d.answer_md[:2500]}" for d in ok_drafts
        )
        rubric_lines = "\n".join(f"- {r}" for r in pack.verification_rubric)
        review_messages = [
            {
                "role": "system",
                "content": (
                    "You are an Anonymous Peer Reviewer on the MetaSearch LLM Council. "
                    f"{pack.council_critic_prompt}\n"
                    f"Verification Rubric:\n{rubric_lines}\n"
                    "Evaluate each anonymized candidate strictly against the numbered sources. "
                    "Respond with STRICT JSON only: "
                    '{"reviews": [{"candidate_id": "Candidate A", "score": 8.5, '
                    '"strengths": "...", "flaws": "..."}]}'
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Question: {question}\n\n"
                    f"Sources:\n{context_text[:4000]}\n\n"
                    f"Anonymized Candidates:\n{anonymized_block}"
                ),
            },
        ]

        reviewer_ov = panel[0]
        gw = (
            self.gateway.with_override(reviewer_ov)
            if hasattr(self.gateway, "with_override")
            else self.gateway
        )
        reviewer_model = (
            reviewer_ov.model
            or reviewer_ov.quick_model
            or getattr(gw, "default_quick_model", self.settings.llm_quick_model)
        )
        reviews_out: list[dict[str, Any]] = []
        llm_scores_by_cand: dict[str, tuple[float, str]] = {}

        try:
            parts: list[str] = []
            async for delta in gw.stream(review_messages, model=reviewer_model):
                if delta.text:
                    parts.append(delta.text)
            raw_review = "".join(parts)
            parsed = extract_json_object(raw_review)
            if parsed:
                if isinstance(parsed.get("scores"), dict):
                    crit_summary = str(parsed.get("critique_summary") or "")
                    for cid, raw_val in parsed["scores"].items():
                        try:
                            val_f = float(raw_val)
                            sc = val_f / 10.0 if val_f > 10.0 else val_f
                            sc = max(0.0, min(10.0, sc))
                            llm_scores_by_cand[str(cid)] = (sc, crit_summary)
                            reviews_out.append(
                                {
                                    "candidate_id": str(cid),
                                    "reviewer": (
                                        f"{reviewer_ov.provider or 'council'}:{reviewer_model}"
                                    ),
                                    "peer_score": round(sc, 2),
                                    "strengths": crit_summary,
                                    "flaws": "",
                                }
                            )
                        except (ValueError, TypeError):
                            continue
                for item in parsed.get("reviews", []):
                    if not isinstance(item, dict):
                        continue
                    cid = str(item.get("candidate_id") or "")
                    raw_sc = item.get("score")
                    try:
                        sc = float(raw_sc) if raw_sc is not None else 7.5
                    except (ValueError, TypeError):
                        sc = 7.5
                    sc = max(0.0, min(10.0, sc))
                    strengths = str(item.get("strengths") or "")
                    flaws = str(item.get("flaws") or "")
                    summary = f"Strengths: {strengths} | Flaws: {flaws}".strip(" |")
                    llm_scores_by_cand[cid] = (sc, summary)
                    reviews_out.append(
                        {
                            "candidate_id": cid,
                            "reviewer": f"{reviewer_ov.provider or 'council'}:{reviewer_model}",
                            "peer_score": round(sc, 2),
                            "strengths": strengths,
                            "flaws": flaws,
                        }
                    )
        except Exception as exc:
            logger.info("council_cross_review_fallback_deterministic", reason=str(exc)[:120])

        for d in ok_drafts:
            if d.candidate_id in llm_scores_by_cand:
                peer_sc, critique = llm_scores_by_cand[d.candidate_id]
                d.peer_score = peer_sc
                d.composite_score = round(0.40 * d.deterministic_score + 0.60 * peer_sc, 2)
                d.peer_critique_summary = critique
            else:
                d.peer_score = d.deterministic_score
                d.composite_score = d.deterministic_score
                d.peer_critique_summary = (
                    f"Citation precision {d.citation_precision:.0%}, "
                    f"source coverage {d.source_coverage:.0%}."
                )
                reviews_out.append(
                    {
                        "candidate_id": d.candidate_id,
                        "reviewer": "deterministic_forensic_auditor",
                        "peer_score": round(d.deterministic_score, 2),
                        "strengths": f"Verified {len(d.valid_citations)} valid citations.",
                        "flaws": (
                            f"Stripped {len(d.invalid_citations)} invalid markers."
                            if d.invalid_citations
                            else "None detected."
                        ),
                    }
                )
        return reviews_out

    async def _synthesize_chairman_verdict(
        self,
        question: str,
        context_text: str,
        ok_drafts: list[PanelistDraft],
        chairman_ov: LLMOverride,
        winner: PanelistDraft,
        pack: DomainIntelligencePack,
    ) -> tuple[str, str]:
        candidates_summary = "\n\n".join(
            f"### {d.candidate_id} (Composite Score: {d.composite_score}/10 | "
            f"Critique: {d.peer_critique_summary})\n{d.answer_md}"
            for d in ok_drafts[:3]
        )
        chairman_messages = [
            {
                "role": "system",
                "content": (
                    f"{SYSTEM_PROMPT}\n"
                    f"You are the Chairman of the MetaSearch LLM Council for {pack.name}. "
                    "Synthesize the final authoritative answer by combining the strongest "
                    "verified insights from the top peer-reviewed candidates below while "
                    "eliminating any flagged flaws. Preserve exact [n] source citations."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Sources:\n\n{context_text}\n\n"
                    f"Peer-Reviewed Council Candidates:\n\n{candidates_summary}\n\n"
                    f"Question: {question}\n\n"
                    "Write the final unified Council Verdict in the same language as the question:"
                ),
            },
        ]
        gw = (
            self.gateway.with_override(chairman_ov)
            if hasattr(self.gateway, "with_override")
            else self.gateway
        )
        chairman_model = (
            chairman_ov.model
            or chairman_ov.report_model
            or chairman_ov.quick_model
            or getattr(gw, "default_report_model", self.settings.llm_report_model)
        )
        chairman_prov = chairman_ov.provider or getattr(gw, "provider", self.settings.llm_provider)
        try:
            parts: list[str] = []
            async for delta in gw.stream(chairman_messages, model=chairman_model, task="report"):
                if delta.text:
                    parts.append(delta.text)
            verdict = "".join(parts).strip()
            if verdict:
                return verdict, f"{chairman_prov}:{chairman_model}"
        except Exception as exc:
            logger.info("chairman_synthesis_fallback_to_winner", reason=str(exc)[:120])
        return winner.answer_md, f"{winner.provider}:{winner.model}"


def _compute_consensus_agreement(ok_drafts: list[PanelistDraft]) -> float:
    """Measure citation & content overlap across council panelists (0..1).

    Uses pairwise average Jaccard similarity across panelists' citation sets
    combined with score spread harmony.
    """
    if len(ok_drafts) <= 1:
        return 1.0
    citation_sets = [set(d.valid_citations) for d in ok_drafts if d.valid_citations]
    if len(citation_sets) >= 2:
        pair_jaccards: list[float] = []
        for i in range(len(citation_sets)):
            for j in range(i + 1, len(citation_sets)):
                union_len = len(citation_sets[i].union(citation_sets[j]))
                inter_len = len(citation_sets[i].intersection(citation_sets[j]))
                pair_jaccards.append(inter_len / union_len if union_len > 0 else 1.0)
        cite_jaccard = sum(pair_jaccards) / len(pair_jaccards) if pair_jaccards else 0.5
    else:
        cite_jaccard = 0.5

    scores = [d.composite_score for d in ok_drafts]
    spread = max(scores) - min(scores)
    score_harmony = max(0.0, 1.0 - (spread / 10.0))
    return round(0.5 * cite_jaccard + 0.5 * score_harmony, 4)
