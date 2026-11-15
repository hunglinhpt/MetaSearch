"""API Key Format & Live Endpoint Validator + Provider Health Telemetry (OmniRoute-inspired).

Provides strict syntactic/placeholder validation and SSRF-safe live probing
for all configured LLM providers in ``LLM_PROVIDER_PRESETS``, plus EWMA latency
and auth-health tracking used by ``app.llm.router.LLMInferenceRouter``.

Security invariants:
- Raw API keys are NEVER written to logs, exceptions, or return payloads
  (always passed through :func:`app.llm.providers.mask_api_key`).
- All non-preset/private URLs are validated with :func:`validate_llm_base_url`.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.config import Settings
from app.core.errors import MetaSearchError
from app.core.logging import get_logger
from app.http import get_http_client
from app.llm.providers import (
    LLM_PROVIDER_PRESETS,
    LLMOverride,
    ResolvedLLMTarget,
    mask_api_key,
    resolve_llm_target,
    validate_llm_base_url,
)
from app.retrieval.health import EngineBreakerRegistry, Outcome

logger = get_logger(__name__)


def _llm_breakers() -> EngineBreakerRegistry:
    from app.llm.gateway import llm_breakers

    return llm_breakers


_MAX_PROBE_BODY_BYTES = 65_536
_EWMA_ALPHA = 0.30

_PLACEHOLDER_MARKERS = (
    "changeme",
    "replace_me",
    "replaceme",
    "your_api_key",
    "your-api-key",
    "your_key",
    "your-key",
    "placeholder",
    "dummy",
    "example",
    "sample",
    "invalid",
    "sk-test",
    "sk-proj-test",
    "test-key",
    "test_key",
    "fake",
    "none",
    "null",
    "undefined",
    "insert_",
)

_WHITESPACE_RE = re.compile(r"\s")


@dataclass(frozen=True, slots=True)
class KeyFormatSpec:
    """Expected key syntax constraints per provider preset."""

    prefixes: tuple[str, ...] = ()
    min_len: int = 16
    max_len: int = 512


KEY_FORMAT_SPECS: dict[str, KeyFormatSpec] = {
    "openai": KeyFormatSpec(prefixes=("sk-", "sk-proj-", "sk-svcacct-"), min_len=32),
    "anthropic": KeyFormatSpec(prefixes=("sk-ant-",), min_len=40),
    "openrouter": KeyFormatSpec(prefixes=("sk-or-",), min_len=32),
    "gemini": KeyFormatSpec(prefixes=("AIza",), min_len=30),
    "groq": KeyFormatSpec(prefixes=("gsk_",), min_len=30),
    "nvidia_nim": KeyFormatSpec(prefixes=("nvapi-",), min_len=30),
    "cerebras": KeyFormatSpec(prefixes=("csk-",), min_len=24),
    "sambanova": KeyFormatSpec(prefixes=(), min_len=24),
    "mistral": KeyFormatSpec(prefixes=(), min_len=24),
    "github_models": KeyFormatSpec(
        prefixes=("ghp_", "github_pat_", "gho_", "ghu_", "ghs_"), min_len=30
    ),
    "cloudflare": KeyFormatSpec(prefixes=(), min_len=24),
    "cohere": KeyFormatSpec(prefixes=(), min_len=24),
    "chutes": KeyFormatSpec(prefixes=(), min_len=24),
    "siliconflow": KeyFormatSpec(prefixes=(), min_len=24),
    "hyperbolic": KeyFormatSpec(prefixes=(), min_len=20),
    "huggingface": KeyFormatSpec(prefixes=("hf_",), min_len=20),
    "custom": KeyFormatSpec(prefixes=(), min_len=8),
}


@dataclass(frozen=True, slots=True)
class KeyFormatResult:
    """Result of offline syntactic/placeholder validation."""

    provider: str
    configured: bool
    format_valid: bool
    reason: str
    masked_key: str


@dataclass(slots=True)
class ProviderValidationResult:
    """Combined format + live connectivity/auth probe result for one provider."""

    provider: str
    name: str
    protocol: str
    base_url: str
    requires_api_key: bool
    free_tier_type: str
    configured: bool
    format_valid: bool
    format_reason: str
    masked_key: str
    key_valid: bool | None = None
    reachable: bool | None = None
    rate_limited: bool = False
    status_code: int | None = None
    latency_ms: int | None = None
    models_count: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "name": self.name,
            "protocol": self.protocol,
            "base_url": self.base_url,
            "requires_api_key": self.requires_api_key,
            "free_tier_type": self.free_tier_type,
            "configured": self.configured,
            "format_valid": self.format_valid,
            "format_reason": self.format_reason,
            "masked_key": self.masked_key,
            "key_valid": self.key_valid,
            "reachable": self.reachable,
            "rate_limited": self.rate_limited,
            "status_code": self.status_code,
            "latency_ms": self.latency_ms,
            "models_count": self.models_count,
            "error": self.error,
        }


@dataclass(slots=True)
class ProviderHealthState:
    """Runtime health & latency telemetry accumulated from probes and live traffic."""

    provider: str
    last_probe_ok: bool | None = None
    key_valid: bool | None = None
    reachable: bool | None = None
    last_probe_at: float | None = None
    last_probe_latency_ms: int | None = None
    last_error: str | None = None
    models_count: int | None = None
    ewma_latency_ms: float | None = None
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_status_code: int | None = None
    last_call_at: float | None = None


class ProviderHealthTracker:
    """Thread-safe health & EWMA latency ledger shared by validator and router."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, ProviderHealthState] = {}

    def get(self, provider: str) -> ProviderHealthState:
        with self._lock:
            st = self._states.get(provider)
            if st is None:
                st = ProviderHealthState(provider=provider)
                self._states[provider] = st
            return ProviderHealthState(
                provider=st.provider,
                last_probe_ok=st.last_probe_ok,
                key_valid=st.key_valid,
                reachable=st.reachable,
                last_probe_at=st.last_probe_at,
                last_probe_latency_ms=st.last_probe_latency_ms,
                last_error=st.last_error,
                models_count=st.models_count,
                ewma_latency_ms=st.ewma_latency_ms,
                successes=st.successes,
                failures=st.failures,
                consecutive_failures=st.consecutive_failures,
                last_status_code=st.last_status_code,
                last_call_at=st.last_call_at,
            )

    def record_probe(self, result: ProviderValidationResult) -> None:
        now = time.time()
        with self._lock:
            st = self._states.setdefault(
                result.provider, ProviderHealthState(provider=result.provider)
            )
            st.last_probe_at = now
            st.last_probe_ok = bool(result.key_valid) if result.key_valid is not None else None
            st.key_valid = result.key_valid
            st.reachable = result.reachable
            st.last_probe_latency_ms = result.latency_ms
            st.models_count = result.models_count
            st.last_status_code = result.status_code
            st.last_error = result.error
            if result.latency_ms is not None and result.reachable:
                lat = float(result.latency_ms)
                st.ewma_latency_ms = (
                    lat
                    if st.ewma_latency_ms is None
                    else (_EWMA_ALPHA * lat + (1.0 - _EWMA_ALPHA) * st.ewma_latency_ms)
                )

    def record_call_outcome(
        self,
        provider: str,
        *,
        ok: bool,
        latency_ms: float | None = None,
        status_code: int | None = None,
        error: str | None = None,
    ) -> None:
        now = time.time()
        with self._lock:
            st = self._states.setdefault(provider, ProviderHealthState(provider=provider))
            st.last_call_at = now
            if status_code is not None:
                st.last_status_code = status_code
            if latency_ms is not None:
                lat = max(0.0, float(latency_ms))
                st.ewma_latency_ms = (
                    lat
                    if st.ewma_latency_ms is None
                    else (_EWMA_ALPHA * lat + (1.0 - _EWMA_ALPHA) * st.ewma_latency_ms)
                )
            if ok:
                st.successes += 1
                st.consecutive_failures = 0
                st.key_valid = True
                st.reachable = True
                st.last_error = None
            else:
                st.failures += 1
                st.consecutive_failures += 1
                if status_code in (401, 403):
                    st.key_valid = False
                    st.reachable = True
                elif status_code == 429:
                    st.key_valid = True
                    st.reachable = True
                st.last_error = error[:200] if error else st.last_error

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                pid: {
                    "provider": st.provider,
                    "last_probe_ok": st.last_probe_ok,
                    "key_valid": st.key_valid,
                    "reachable": st.reachable,
                    "last_probe_at": st.last_probe_at,
                    "last_probe_latency_ms": st.last_probe_latency_ms,
                    "ewma_latency_ms": (
                        round(st.ewma_latency_ms, 1) if st.ewma_latency_ms is not None else None
                    ),
                    "models_count": st.models_count,
                    "successes": st.successes,
                    "failures": st.failures,
                    "consecutive_failures": st.consecutive_failures,
                    "last_status_code": st.last_status_code,
                    "last_error": st.last_error,
                }
                for pid, st in sorted(self._states.items())
            }

    def reset(self) -> None:
        with self._lock:
            self._states.clear()


_health_tracker = ProviderHealthTracker()


def get_provider_health_tracker() -> ProviderHealthTracker:
    return _health_tracker


def _is_placeholder_key(key: str) -> str | None:
    """Return reason string if ``key`` looks like a dummy/placeholder token."""
    lower = key.lower()
    if _WHITESPACE_RE.search(key):
        return "contains_whitespace"
    if not key.isascii():
        return "non_ascii_characters"
    for marker in _PLACEHOLDER_MARKERS:
        if marker in lower:
            return f"placeholder_marker:{marker}"
    if len(set(key)) < 6:
        return "low_entropy_repeated_chars"
    return None


def validate_key_format(provider: str, api_key: str) -> KeyFormatResult:
    """Validate API key presence, length, prefix, and non-placeholder sanity offline."""
    preset = LLM_PROVIDER_PRESETS.get(provider)
    cleaned = (api_key or "").strip()
    masked = mask_api_key(cleaned)

    if preset is None:
        return KeyFormatResult(
            provider=provider,
            configured=bool(cleaned),
            format_valid=bool(cleaned),
            reason="unknown_provider",
            masked_key=masked,
        )

    if not preset.requires_api_key:
        return KeyFormatResult(
            provider=provider,
            configured=True,
            format_valid=True,
            reason="keyless_or_local",
            masked_key=masked,
        )

    if not cleaned:
        return KeyFormatResult(
            provider=provider,
            configured=False,
            format_valid=False,
            reason="missing_api_key",
            masked_key="",
        )

    placeholder = _is_placeholder_key(cleaned)
    if placeholder is not None:
        return KeyFormatResult(
            provider=provider,
            configured=True,
            format_valid=False,
            reason=placeholder,
            masked_key=masked,
        )

    spec = KEY_FORMAT_SPECS.get(provider, KeyFormatSpec())
    if len(cleaned) < spec.min_len:
        return KeyFormatResult(
            provider=provider,
            configured=True,
            format_valid=False,
            reason=f"too_short_min_{spec.min_len}",
            masked_key=masked,
        )
    if len(cleaned) > spec.max_len:
        return KeyFormatResult(
            provider=provider,
            configured=True,
            format_valid=False,
            reason=f"too_long_max_{spec.max_len}",
            masked_key=masked,
        )
    if spec.prefixes and not cleaned.startswith(spec.prefixes):
        return KeyFormatResult(
            provider=provider,
            configured=True,
            format_valid=False,
            reason=f"expected_prefix:{'|'.join(spec.prefixes)}",
            masked_key=masked,
        )

    return KeyFormatResult(
        provider=provider,
        configured=True,
        format_valid=True,
        reason="ok",
        masked_key=masked,
    )


def _sanitize_base_url_for_display(url: str) -> str:
    """Strip userinfo, query parameters, and fragments before returning/logging a URL."""
    if not url:
        return ""
    try:
        p = urlsplit(url)
    except ValueError:
        return ""
    if not p.scheme or not p.hostname:
        return url.split("?")[0].split("#")[0]
    host = f"[{p.hostname}]" if ":" in p.hostname else p.hostname
    netloc = f"{host}:{p.port}" if p.port else host
    return urlunsplit((p.scheme, netloc, p.path.rstrip("/"), "", ""))


def _probe_headers(target: ResolvedLLMTarget) -> dict[str, str]:
    headers: dict[str, str] = {"content-type": "application/json"}
    if target.protocol == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        if target.api_key:
            headers["x-api-key"] = target.api_key
    else:
        if target.api_key and target.provider not in ("pollinations", "ollama"):
            headers["Authorization"] = f"Bearer {target.api_key}"
    for k, v in target.extra_headers.items():
        if "\r" in k or "\n" in k or "\r" in v or "\n" in v:
            continue
        headers[k] = v
    return headers


def scoped_runtime_override(
    runtime_override: LLMOverride | None,
    provider: str,
    settings: Settings,
) -> LLMOverride | None:
    """Return ``runtime_override`` only when it targets ``provider`` to avoid cross-key leakage."""
    if runtime_override is None:
        return None
    eff_rt_provider = runtime_override.provider or settings.llm_provider
    return runtime_override if eff_rt_provider == provider else None


def _probe_models_url(target: ResolvedLLMTarget) -> str:
    base = target.base_url.rstrip("/")
    if target.protocol == "anthropic":
        return f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
    if target.provider == "openrouter" and "openrouter.ai" in base:
        return f"{base}/auth/key"
    return f"{base}/models"


def _probe_chat_url(target: ResolvedLLMTarget) -> str:
    base = target.base_url.rstrip("/")
    if target.protocol == "anthropic":
        return f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"
    return f"{base}/chat/completions"


def _extract_models_count(raw: bytes) -> int | None:
    try:
        payload = json.loads(raw[:_MAX_PROBE_BODY_BYTES].decode(errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return len(data)
        models = payload.get("models")
        if isinstance(models, list):
            return len(models)
    if isinstance(payload, list):
        return len(payload)
    return None


def _parse_retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


async def validate_provider_key(
    provider: str,
    settings: Settings,
    *,
    runtime_override: LLMOverride | None = None,
    request_override: LLMOverride | None = None,
    dry_run: bool = False,
    timeout_s: float | None = None,
    client: httpx.AsyncClient | None = None,
    send_minimal_completion: bool = False,
) -> ProviderValidationResult:
    """Validate format and (optionally) probe live auth/connectivity for ``provider``.

    Always safe to call on disabled/unconfigured presets; never throws raw keys
    or full secret-bearing URLs into exceptions or logs.
    """
    preset = LLM_PROVIDER_PRESETS.get(provider, LLM_PROVIDER_PRESETS["custom"])
    eff_req = request_override or LLMOverride(provider=provider)  # type: ignore[arg-type]
    if eff_req.provider is None:
        eff_req = eff_req.model_copy(update={"provider": provider})

    eff_runtime = scoped_runtime_override(runtime_override, provider, settings)
    try:
        target = resolve_llm_target(
            settings, runtime_override=eff_runtime, request_override=eff_req
        )
    except MetaSearchError as exc:
        res = ProviderValidationResult(
            provider=provider,
            name=preset.name,
            protocol=preset.protocol,
            base_url="",
            requires_api_key=preset.requires_api_key,
            free_tier_type=preset.free_tier_type,
            configured=False,
            format_valid=False,
            format_reason="target_resolution_rejected",
            masked_key="",
            key_valid=False,
            reachable=False,
            error=exc.message[:160],
        )
        _health_tracker.record_probe(res)
        return res

    # Re-read raw env key without synthetic placeholder suffixes applied by resolve_llm_target
    raw_key = target.api_key
    if target.provider in ("pollinations", "ollama") and raw_key in (
        "pollinations-free",
        "ollama-local",
        f"{target.provider}-free",
    ):
        raw_key = ""

    fmt = validate_key_format(target.provider, raw_key)
    if target.provider == "custom" and not target.base_url:
        fmt = KeyFormatResult(
            provider="custom",
            configured=False,
            format_valid=False,
            reason="missing_base_url",
            masked_key=fmt.masked_key,
        )

    res = ProviderValidationResult(
        provider=target.provider,
        name=preset.name,
        protocol=target.protocol,
        base_url=_sanitize_base_url_for_display(target.base_url),
        requires_api_key=preset.requires_api_key,
        free_tier_type=preset.free_tier_type,
        configured=fmt.configured,
        format_valid=fmt.format_valid,
        format_reason=fmt.reason,
        masked_key=fmt.masked_key,
    )

    if target.provider == "none" or target.protocol == "none":
        res.key_valid = None
        res.reachable = None
        res.error = "provider_disabled"
        _health_tracker.record_probe(res)
        return res

    if dry_run:
        return res

    if not target.base_url:
        res.key_valid = False
        res.reachable = False
        res.error = "missing_base_url"
        _health_tracker.record_probe(res)
        return res

    if preset.requires_api_key and not raw_key:
        res.key_valid = False
        res.reachable = False
        res.error = "missing_api_key"
        _health_tracker.record_probe(res)
        return res

    if preset.requires_api_key and (_WHITESPACE_RE.search(raw_key) or not raw_key.isascii()):
        res.key_valid = False
        res.reachable = False
        res.error = "invalid_key_characters"
        _health_tracker.record_probe(res)
        return res

    # SSRF guard on any resolved base_url before network egress.
    # Only Ollama (localhost daemon) may access loopback/private hosts implicitly;
    # all custom/env/runtime/request endpoints must satisfy strict public-host / allowlist policy.
    try:
        validate_llm_base_url(
            target.base_url,
            allow_private=(target.provider == "ollama"),
            allowed_hosts=settings.llm_byo_allowed_hosts,
            private_hosts=settings.llm_private_hosts,
        )
    except MetaSearchError as exc:
        res.key_valid = False
        res.reachable = False
        res.error = exc.message[:160]
        _health_tracker.record_probe(res)
        return res

    eff_timeout = timeout_s or float(getattr(settings, "llm_key_probe_timeout_s", 5.0))
    req_timeout = httpx.Timeout(eff_timeout)
    headers = _probe_headers(target)
    probe_url = _probe_models_url(target)

    async def _do_probe(http_client: httpx.AsyncClient) -> None:
        started = time.perf_counter()
        try:
            resp = await http_client.get(probe_url, headers=headers, timeout=req_timeout)
            # Fall back to a 1-token chat probe only on 404/405 when explicitly enabled.
            if resp.status_code in (404, 405) and (
                send_minimal_completion or target.provider in ("pollinations", "github_models")
            ):
                chat_url = _probe_chat_url(target)
                body: dict[str, Any] = {
                    "model": target.quick_model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ok"}],
                }
                resp = await http_client.post(
                    chat_url, headers=headers, json=body, timeout=req_timeout
                )

            res.latency_ms = int((time.perf_counter() - started) * 1000)
            res.status_code = resp.status_code
            breaker = _llm_breakers().get(f"llm:{target.provider}")

            if 200 <= resp.status_code < 300:
                res.reachable = True
                res.key_valid = True
                res.models_count = _extract_models_count(resp.content)
                breaker.record(Outcome.OK)
            elif resp.status_code in (401, 403):
                res.reachable = True
                res.key_valid = False
                res.error = f"authentication_failed (HTTP {resp.status_code})"
                breaker.record(Outcome.UPSTREAM_ERROR)
            elif resp.status_code == 429:
                res.reachable = True
                res.key_valid = True
                res.rate_limited = True
                res.error = "rate_limited (HTTP 429)"
                breaker.record(Outcome.RATE_LIMITED, retry_after_s=_parse_retry_after(resp))
            elif resp.status_code == 400 and preset.requires_api_key and not fmt.format_valid:
                res.reachable = True
                res.key_valid = False
                res.error = "bad_request_invalid_key (HTTP 400)"
            else:
                res.reachable = True
                res.key_valid = None
                res.error = f"http_status_{resp.status_code}"
                if resp.status_code >= 500:
                    breaker.record(Outcome.UPSTREAM_ERROR)
        except httpx.TimeoutException:
            res.latency_ms = int((time.perf_counter() - started) * 1000)
            res.reachable = False
            res.key_valid = None
            res.error = "timeout"
            _llm_breakers().get(f"llm:{target.provider}").record(Outcome.TIMEOUT)
        except httpx.HTTPError as exc:
            res.latency_ms = int((time.perf_counter() - started) * 1000)
            res.reachable = False
            res.key_valid = None
            res.error = type(exc).__name__
            _llm_breakers().get(f"llm:{target.provider}").record(Outcome.UPSTREAM_ERROR)

    eff_client = client if client is not None else get_http_client("internal")
    await _do_probe(eff_client)

    _health_tracker.record_probe(res)
    logger.info(
        "llm_key_probed",
        provider=res.provider,
        configured=res.configured,
        format_valid=res.format_valid,
        key_valid=res.key_valid,
        reachable=res.reachable,
        status_code=res.status_code,
        latency_ms=res.latency_ms,
    )
    return res


async def validate_all_configured_providers(
    settings: Settings,
    *,
    runtime_override: LLMOverride | None = None,
    include_unconfigured: bool = False,
    dry_run: bool = False,
    providers: list[str] | None = None,
    timeout_s: float | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[ProviderValidationResult]:
    """Concurrently validate configured (or requested) LLM providers."""
    candidate_ids = (
        list(providers)
        if providers is not None
        else [pid for pid in LLM_PROVIDER_PRESETS if pid != "none"]
    )
    selected: list[str] = []
    for pid in candidate_ids:
        preset = LLM_PROVIDER_PRESETS.get(pid)
        if preset is None or pid == "none":
            continue
        eff_runtime = scoped_runtime_override(runtime_override, pid, settings)
        target = resolve_llm_target(
            settings,
            runtime_override=eff_runtime,
            request_override=LLMOverride(provider=pid),  # type: ignore[arg-type]
        )
        raw_key = target.api_key
        if pid in ("pollinations", "ollama") and raw_key in (
            "pollinations-free",
            "ollama-local",
            f"{pid}-free",
        ):
            raw_key = ""
        is_cfg = (
            bool(raw_key)
            if preset.requires_api_key
            else (bool(target.base_url) if pid == "custom" else True)
        )
        if include_unconfigured or is_cfg or pid == settings.llm_provider:
            selected.append(pid)

    if not selected:
        return []

    probe_sem = asyncio.Semaphore(6)

    async def _probe_one(pid: str, http_client: httpx.AsyncClient) -> ProviderValidationResult:
        async with probe_sem:
            return await validate_provider_key(
                pid,
                settings,
                runtime_override=runtime_override,
                dry_run=dry_run,
                timeout_s=timeout_s,
                client=http_client,
            )

    eff_client = client if client is not None else get_http_client("internal")
    return list(await asyncio.gather(*(_probe_one(p, eff_client) for p in selected)))
