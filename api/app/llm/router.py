"""Multi-Provider Inference Router (OmniRoute-inspired).

Builds ordered failover combos and ranks configured providers by:
1. Key validity & auth health (never dispatching to known 401/403 keys).
2. Circuit breaker availability (``llm_breakers``).
3. EWMA latency and recent failure penalty (``ProviderHealthTracker``).
4. Cost/tier priority (preferring permanent/local free tiers for quick tasks
   unless an explicit chain overrides).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.core.logging import get_logger
from app.llm.key_validator import (
    get_provider_health_tracker,
    scoped_runtime_override,
    validate_key_format,
)
from app.llm.providers import (
    LLM_PROVIDER_PRESETS,
    LLMOverride,
    ResolvedLLMTarget,
    resolve_llm_target,
)
from app.retrieval.health import EngineBreakerRegistry

logger = get_logger(__name__)


def _llm_breakers() -> EngineBreakerRegistry:
    from app.llm.gateway import llm_breakers

    return llm_breakers


_FREE_TIER_RANK: dict[str, int] = {
    "permanent_free": 0,
    "free_credits": 1,
    "trial_credits": 1,
    "local_free": 2,
    "keyless_free": 3,
    "paid_or_byo": 4,
}


@dataclass(frozen=True, slots=True)
class RankedProvider:
    """Routing score & health view for a single LLM provider preset."""

    provider: str
    score: float
    configured: bool
    format_valid: bool
    key_valid: bool | None
    breaker_state: str
    ewma_latency_ms: float | None
    consecutive_failures: int
    free_tier_type: str


def _effective_model_for_target(
    requested_model: str | None,
    target: ResolvedLLMTarget,
    *,
    task: str = "answer",
    request_model_override: str | None = None,
    runtime_model_override: str | None = None,
) -> str:
    if request_model_override:
        return request_model_override
    if runtime_model_override:
        return runtime_model_override
    if not requested_model:
        return target.report_model if task == "report" else target.quick_model
    if target.provider != "openai":
        if requested_model == "gpt-4o-mini":
            return target.quick_model
        if requested_model == "gpt-4o":
            return target.report_model
    return requested_model


def _raw_provider_key(
    settings: Settings,
    provider: str,
    runtime_override: LLMOverride | None = None,
) -> str:
    scoped_rt = scoped_runtime_override(runtime_override, provider, settings)
    target = resolve_llm_target(
        settings,
        runtime_override=scoped_rt,
        request_override=LLMOverride(provider=provider),  # type: ignore[arg-type]
    )
    key = target.api_key
    if provider in ("pollinations", "ollama") and key in (
        "pollinations-free",
        "ollama-local",
        f"{provider}-free",
    ):
        return ""
    return key


def score_provider(
    settings: Settings,
    provider: str,
    *,
    task: str = "answer",
    runtime_override: LLMOverride | None = None,
) -> RankedProvider:
    """Compute a lower-is-better dispatch rank score for ``provider``."""
    preset = LLM_PROVIDER_PRESETS.get(provider, LLM_PROVIDER_PRESETS["custom"])
    tracker = get_provider_health_tracker()
    health = tracker.get(provider)
    breaker = _llm_breakers().get(f"llm:{provider}")
    breaker_state = breaker.state

    scoped_rt = scoped_runtime_override(runtime_override, provider, settings)
    resolved_target = resolve_llm_target(
        settings,
        runtime_override=scoped_rt,
        request_override=LLMOverride(provider=provider),  # type: ignore[arg-type]
    )
    raw_key = resolved_target.api_key
    if provider in ("pollinations", "ollama") and raw_key in (
        "pollinations-free",
        "ollama-local",
        f"{provider}-free",
    ):
        raw_key = ""
    fmt = validate_key_format(provider, raw_key)
    if provider == "custom":
        has_custom_url = bool(resolved_target.base_url)
        configured = has_custom_url
        format_valid = fmt.format_valid and has_custom_url
    elif preset.requires_api_key:
        configured = fmt.configured
        format_valid = fmt.format_valid
    else:
        configured = True
        format_valid = fmt.format_valid

    score = 0.0
    if not configured or provider == "none":
        score += 10_000.0
    elif preset.requires_api_key and not format_valid and health.key_valid is not True:
        score += 5_000.0
    if health.key_valid is False:
        score += 9_000.0
    elif health.key_valid is True:
        score -= 50.0

    if breaker_state == "open":
        score += 4_000.0
    elif breaker_state == "half_open":
        score += 500.0

    tier_rank = _FREE_TIER_RANK.get(preset.free_tier_type, 3)
    if task == "report":
        # Prefer quality over strictly free tier for long report synthesis
        tier_rank = 1 if preset.free_tier_type == "paid_or_byo" else tier_rank
    score += tier_rank * 10.0

    if health.ewma_latency_ms is not None:
        score += min(health.ewma_latency_ms, 10_000.0) / 100.0
    else:
        score += 8.0  # neutral unmeasured baseline (~800ms prior)

    score += min(health.consecutive_failures, 10) * 80.0

    return RankedProvider(
        provider=provider,
        score=round(score, 2),
        configured=configured,
        format_valid=format_valid,
        key_valid=health.key_valid,
        breaker_state=breaker_state,
        ewma_latency_ms=round(health.ewma_latency_ms, 1) if health.ewma_latency_ms else None,
        consecutive_failures=health.consecutive_failures,
        free_tier_type=preset.free_tier_type,
    )


def rank_providers(
    settings: Settings,
    *,
    task: str = "answer",
    include_unconfigured: bool = False,
    runtime_override: LLMOverride | None = None,
) -> list[RankedProvider]:
    """Return providers ordered from best to worst dispatch candidate."""
    primary = (runtime_override.provider if runtime_override else None) or settings.llm_provider
    ranked: list[RankedProvider] = []
    for pid in LLM_PROVIDER_PRESETS:
        if pid == "none":
            continue
        rp = score_provider(settings, pid, task=task, runtime_override=runtime_override)
        if include_unconfigured or rp.configured or pid == primary:
            ranked.append(rp)
    ranked.sort(key=lambda item: (item.score, item.provider))
    return ranked


class LLMInferenceRouter:
    """OmniRoute-style Combo builder and failover planner for ``LLMGateway``."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def build_combo(
        self,
        target: ResolvedLLMTarget,
        eff_model: str,
        *,
        task: str = "answer",
        request_override: LLMOverride | None = None,
        runtime_override: LLMOverride | None = None,
    ) -> list[tuple[ResolvedLLMTarget, str]]:
        """Assemble ordered ``(target, model)`` candidates for one request.

        Order:
          1. Resolved primary ``(target, eff_model)``.
          2. OpenRouter ``fallback_models`` if configured on OpenRouter primary.
          3. Explicit ``target.fallback_provider`` (always honored if set).
          4. Configured & healthy fallback chain / auto-discovered providers
             when ``llm_auto_fallback`` is enabled and the caller did not pin
             custom BYO credentials (preventing unintended data exfiltration).
        """
        candidates: list[tuple[ResolvedLLMTarget, str]] = [(target, eff_model)]
        seen_providers: set[str] = {target.provider}

        if target.provider == "openrouter" and target.api_key and target.fallback_models:
            for alt_model in target.fallback_models:
                if alt_model != eff_model:
                    candidates.append((target, alt_model))

        if (
            target.fallback_provider
            and target.fallback_provider != "none"
            and target.fallback_provider not in seen_providers
        ):
            try:
                fb_target = resolve_llm_target(
                    self._settings,
                    request_override=LLMOverride(provider=target.fallback_provider),  # type: ignore[arg-type]
                )
                candidates.append(
                    (
                        fb_target,
                        _effective_model_for_target(
                            None,
                            fb_target,
                            task=task,
                            request_model_override=(
                                request_override.model if request_override else None
                            ),
                            runtime_model_override=(
                                runtime_override.model if runtime_override else None
                            ),
                        ),
                    )
                )
                seen_providers.add(fb_target.provider)
            except Exception as exc:
                logger.warning("llm_fallback_target_unresolvable", error=str(exc)[:120])

        # Privacy guard: never redirect a custom BYO endpoint request to other providers
        has_byo_creds = bool(
            request_override and (request_override.base_url or request_override.api_key is not None)
        )
        if not getattr(self._settings, "llm_auto_fallback", True) or has_byo_creds:
            return candidates

        tracker = get_provider_health_tracker()
        explicit_chain = [
            p.strip()
            for p in (getattr(self._settings, "llm_fallback_chain", "") or "").split(",")
            if p.strip()
        ]
        allow_keyless = bool(getattr(self._settings, "llm_allow_keyless_fallback", False))

        if explicit_chain:
            fallback_order = explicit_chain
        else:
            fallback_order = [r.provider for r in rank_providers(self._settings, task=task)]

        for pid in fallback_order:
            if pid in seen_providers or pid == "none":
                continue
            preset = LLM_PROVIDER_PRESETS.get(pid)
            if preset is None:
                continue
            # Implicit auto-discovery must not send user queries to public keyless
            # endpoints unless explicitly allowed or named in LLM_FALLBACK_CHAIN.
            if not explicit_chain:
                if pid == "pollinations" and not allow_keyless:
                    continue
                if pid == "ollama":
                    st = tracker.get("ollama")
                    if st.reachable is not True:
                        continue
            health = tracker.get(pid)
            if health.key_valid is False:
                continue
            raw_key = _raw_provider_key(self._settings, pid)
            if preset.requires_api_key:
                fmt = validate_key_format(pid, raw_key)
                if not explicit_chain and not fmt.format_valid and health.key_valid is not True:
                    continue
                if explicit_chain and not raw_key:
                    continue
            try:
                fb_target = resolve_llm_target(
                    self._settings,
                    request_override=LLMOverride(provider=pid),  # type: ignore[arg-type]
                )
                if not fb_target.base_url:
                    continue
                fb_model = fb_target.report_model if task == "report" else fb_target.quick_model
                candidates.append((fb_target, fb_model))
                seen_providers.add(pid)
            except Exception as exc:
                logger.debug("llm_combo_candidate_skipped", provider=pid, error=type(exc).__name__)
                continue

        return candidates


def route_summary(
    settings: Settings,
    *,
    task: str = "answer",
    runtime_override: LLMOverride | None = None,
) -> dict[str, Any]:
    """Structured routing & health snapshot for ``GET /v1/llm/health``."""
    tracker = get_provider_health_tracker().snapshot()
    ranked = rank_providers(
        settings, task=task, include_unconfigured=True, runtime_override=runtime_override
    )
    primary = (runtime_override.provider if runtime_override else None) or settings.llm_provider
    return {
        "primary_provider": primary,
        "explicit_fallback_provider": settings.llm_fallback_provider or None,
        "auto_fallback_enabled": getattr(settings, "llm_auto_fallback", True),
        "allow_keyless_fallback": getattr(settings, "llm_allow_keyless_fallback", False),
        "fallback_chain": [
            p.strip()
            for p in (getattr(settings, "llm_fallback_chain", "") or "").split(",")
            if p.strip()
        ],
        "ranked_providers": [
            {
                "provider": r.provider,
                "score": r.score,
                "configured": r.configured,
                "format_valid": r.format_valid,
                "key_valid": r.key_valid,
                "breaker_state": r.breaker_state,
                "ewma_latency_ms": r.ewma_latency_ms,
                "consecutive_failures": r.consecutive_failures,
                "free_tier_type": r.free_tier_type,
                "telemetry": tracker.get(r.provider),
            }
            for r in ranked
        ],
    }
