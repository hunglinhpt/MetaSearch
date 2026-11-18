"""Provider-agnostic LLM gateway (docs/02-system-architecture.md, LLM Gateway).

Supports:
- **OpenRouter Free Router** (``openrouter/free`` — https://openrouter.ai/openrouter/free)
- **Pollinations AI** (keyless zero-config free OpenAI-compatible endpoint)
- **Google Gemini AI Studio**, **Groq Cloud LPU**, **Cerebras**, **Mistral**,
  **GitHub Models**, **HuggingFace Router** (reputable free tiers)
- **Local Self-Hosted** (Ollama, vLLM, LM Studio, llama.cpp)
- **Custom Enterprise / External System LLM** (LiteLLM, Kong, Azure, custom URL + headers)
- **OpenAI** ``/chat/completions`` & **Anthropic** ``/v1/messages``

Streaming yields ``StreamDelta`` so SSE can forward tokens as they arrive and
still record token usage + estimated cost afterwards.

WP-08 resilience (readiness work packages):
- Errors are classified: 400/401/403/404 are **non-retryable** and never
  fail over (same key would fail the same way); 429 honors ``Retry-After``;
  5xx/timeout/transport fail over to the next candidate.
- Total upstream calls per request are bounded by ``llm_max_attempts``.
- A per-provider circuit breaker (reusing ``EngineBreaker``) skips providers
  that are failing; open panelists are skipped in the council too.
- ``StreamDelta.finish_reason`` carries the upstream stop reason so
  ``length`` becomes ``status="truncated"`` downstream.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.core.logging import get_logger
from app.core.metrics import LLM_COST, LLM_REQUESTS, LLM_TOKENS, LLM_TTFT, STAGE_DURATION
from app.http import POOL_ACQUIRE_TIMEOUT_S, get_http_client
from app.llm.endpoint_policy import llm_endpoint_is_trusted
from app.llm.providers import (
    LLMOverride,
    ResolvedLLMTarget,
    get_runtime_llm_store,
    resolve_llm_target,
)
from app.retrieval.health import EngineBreakerRegistry, Outcome

logger = get_logger(__name__)

_trace_tasks: set[asyncio.Task[None]] = set()

# USD per 1M tokens (input, output). Unknown or free models cost-estimate at 0.
_PRICE_TABLE: dict[str, tuple[float, float]] = {
    "openrouter/free": (0.0, 0.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "claude-3-5-haiku-latest": (0.80, 4.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
}

# Cap for reading error/JSON bodies so a hostile upstream cannot balloon memory.
_MAX_BODY_BYTES = 1_000_000

_NON_RETRYABLE_STATUSES = {400, 401, 403, 404}

# Breakers keyed "llm:{provider}" — shared per process, council included.
llm_breakers = EngineBreakerRegistry(threshold=3, timeout_threshold=5)


class LLMError(Exception):
    """Raised when the upstream LLM fails or is not configured."""


class LLMTimeoutError(LLMError, TimeoutError):
    """Raised when an LLM upstream call times out.

    ``provider_fault`` is False when the request budget cut the call shorter than the
    provider deserves: that timeout is our deadline, not a sign the provider is unhealthy.
    """

    def __init__(self, message: str, *, provider_fault: bool = True) -> None:
        super().__init__(message)
        self.provider_fault = provider_fault


class LLMUnavailableError(LLMError):
    """Every candidate provider sits behind an open breaker: try again after a while."""

    code = "overloaded"

    def __init__(self, message: str, *, retry_after_s: float) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class LLMStatusError(LLMError):
    """Upstream returned an HTTP status; carries retry classification."""

    def __init__(
        self,
        provider: str,
        status_code: int,
        *,
        retry_after_s: float | None = None,
    ) -> None:
        self.provider = provider
        self.status_code = status_code
        self.retry_after_s = retry_after_s
        self.retryable = status_code not in _NON_RETRYABLE_STATUSES
        super().__init__(f"LLM provider '{provider}' returned HTTP {status_code}")


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def cost_usd(self, model: str) -> float:
        m = (model or "").strip().lower()
        if not m or m == "openrouter/free" or m.endswith(":free"):
            return 0.0
        prices = _PRICE_TABLE.get(model)
        if prices is None:
            # Prefix match so dated snapshots ("-2024-07-18") still price.
            prices = next(
                (v for k, v in _PRICE_TABLE.items() if model.startswith(k)),
                None,
            ) or (0.0, 0.0)
        return (self.input_tokens / 1e6 * prices[0]) + (self.output_tokens / 1e6 * prices[1])


@dataclass(slots=True)
class StreamDelta:
    text: str | None = None
    usage: Usage | None = None  # set on the final delta only
    finish_reason: str | None = None  # "stop" | "length" | provider-specific


async def _sse_payloads(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    """Yield JSON payloads from an httpx SSE line stream (``data: {...}``)."""
    async for line in lines:
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            logger.warning("llm_sse_bad_json", preview=data[:120])


async def _read_limited(resp: httpx.Response, limit: int = _MAX_BODY_BYTES) -> str:
    """Read a response body with a hard byte cap (REL-10: unbounded aread)."""
    content_length = resp.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > limit:
        raise LLMError(f"upstream body {content_length}B exceeds {limit}B limit")
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes(65536):
        total += len(chunk)
        chunks.append(chunk)
        if total >= limit:
            break
    return b"".join(chunks).decode(errors="replace")


class LLMGateway:
    """Single entrypoint used by the synthesizer; provider chosen from settings or overrides."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        *,
        request_override: LLMOverride | None = None,
        use_runtime_store: bool = True,
    ) -> None:
        self._settings = settings
        self.settings = settings
        self._client = client
        self._request_override = request_override
        self._use_runtime_store = use_runtime_store

    def with_override(self, override: LLMOverride | None) -> LLMGateway:
        """Return a gateway view isolated to ``override`` for a single request."""
        if override is None:
            return self
        merged = override
        if self._request_override is not None:
            data = self._request_override.model_dump(exclude_none=True)
            data.update(override.model_dump(exclude_none=True))
            merged = LLMOverride(**data)
        return LLMGateway(
            self._settings,
            client=self._client,
            request_override=merged,
            use_runtime_store=self._use_runtime_store,
        )

    def resolve_target(self) -> ResolvedLLMTarget:
        runtime_ov = get_runtime_llm_store().get_override() if self._use_runtime_store else None
        return resolve_llm_target(
            self._settings,
            runtime_override=runtime_ov,
            request_override=self._request_override,
        )

    @property
    def provider(self) -> str:
        return self.resolve_target().provider

    @property
    def default_quick_model(self) -> str:
        return self.resolve_target().quick_model

    @property
    def default_report_model(self) -> str:
        return self.resolve_target().report_model

    def _effective_model(
        self,
        requested_model: str | None,
        target: ResolvedLLMTarget,
        *,
        task: str = "answer",
    ) -> str:
        if self._request_override and self._request_override.model:
            return self._request_override.model
        runtime_ov = get_runtime_llm_store().get_override() if self._use_runtime_store else None
        if runtime_ov and runtime_ov.model:
            return runtime_ov.model
        if not requested_model:
            return target.report_model if task == "report" else target.quick_model
        if target.provider != "openai":
            if requested_model == "gpt-4o-mini":
                return target.quick_model
            if requested_model == "gpt-4o":
                return target.report_model
        return requested_model

    def _headers_for(self, target: ResolvedLLMTarget) -> dict[str, str]:
        if target.protocol == "anthropic":
            headers: dict[str, str] = {
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            if target.api_key:
                headers["x-api-key"] = target.api_key
        else:
            headers = {"content-type": "application/json"}
            if target.api_key:
                headers["Authorization"] = f"Bearer {target.api_key}"

        # Strip auth-bearing headers from extra_headers when target has no explicit API key
        for k, v in target.extra_headers.items():
            k_lower = k.lower()
            if not target.api_key and k_lower in ("authorization", "x-api-key", "cookie"):
                continue
            headers[k] = v
        return headers

    def _body_for(
        self,
        target: ResolvedLLMTarget,
        messages: list[dict[str, str]],
        model: str,
        *,
        max_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        if target.protocol == "anthropic":
            system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
            return {
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system": system,
                "stream": True,
                "messages": [m for m in messages if m["role"] != "system"],
            }
        payload: dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "messages": messages,
        }
        if target.provider not in ("pollinations",):
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _url_for(self, target: ResolvedLLMTarget) -> str:
        base = target.base_url.rstrip("/")
        if target.protocol == "anthropic":
            return f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"
        return f"{base}/chat/completions"

    def _candidates(
        self,
        target: ResolvedLLMTarget,
        messages: list[dict[str, str]],
        eff_model: str,
        *,
        max_tokens: int,
        temperature: float,
        task: str,
    ) -> list[tuple[ResolvedLLMTarget, str]]:
        """Ordered failover candidates via OmniRoute-style combo builder."""
        from app.llm.router import LLMInferenceRouter

        runtime_ov = get_runtime_llm_store().get_override() if self._use_runtime_store else None
        combo = LLMInferenceRouter(self._settings).build_combo(
            target,
            eff_model,
            task=task,
            request_override=self._request_override,
            runtime_override=runtime_ov,
        )
        # docs/30 Part 1 (P1-1): LLM Endpoint Privacy Policy gates EVERY
        # candidate — static preset chains AND override-injected targets.
        # The fallback chain (llm_fallback_provider / llm_fallback_chain /
        # llm_auto_fallback) previously dispatched prompts to untrusted hosts
        # (groq/openrouter/mistral/...) with no sweep and no runtime gate.
        # Static config is ALSO fail-fast validated at startup (main.py sweep
        # enumerates the full provider surface), so this runtime guard is
        # defense-in-depth against hot-config drift.
        policy = getattr(self._settings, "llm_endpoint_policy", "trusted_only")
        if policy == "trusted_only":
            trusted = [(t, u) for (t, u) in combo if llm_endpoint_is_trusted(t.base_url)]
            if not trusted and combo:
                logger.error(
                    "llm_endpoint_policy_blocked_all",
                    policy=policy,
                    providers=[t.provider for t, _ in combo],
                )
                raise LLMError(
                    "all LLM endpoints rejected by the trusted-provider allowlist "
                    "(docs/30 Part 1); configure an enterprise/local provider or "
                    "set LLM_ENDPOINT_POLICY=open"
                )
            if len(trusted) != len(combo):
                logger.warning(
                    "llm_endpoint_policy_dropped_candidates",
                    dropped=[t.provider for t, _ in combo if (t, _) not in trusted],
                )
            combo = trusted
        return combo

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        task: str = "answer",
    ) -> AsyncIterator[StreamDelta]:
        """Stream answer deltas with classified errors and a bounded attempt budget.

        REL-10: every upstream call consumes one attempt from
        ``llm_max_attempts`` (default 3), regardless of how many fallback
        candidates exist. Non-retryable statuses (400/401/403/404) raise
        immediately. 429 sleeps ``Retry-After`` before consuming the next
        attempt on the same candidate. Open panelist breakers are skipped
        without consuming attempts.
        """
        target = self.resolve_target()
        if target.provider == "none" or target.protocol == "none":
            if target.fallback_provider and target.fallback_provider != "none":
                target = resolve_llm_target(
                    self._settings,
                    request_override=LLMOverride(provider=target.fallback_provider),  # type: ignore[arg-type]
                )
            else:
                raise LLMError(
                    "LLM provider is 'none': set LLM_PROVIDER plus an API key to use /v1/answer"
                )

        eff_model = self._effective_model(model, target, task=task)
        max_tok = max_tokens or self._settings.llm_answer_max_tokens
        temp = self._settings.llm_temperature if temperature is None else temperature
        max_attempts = max(int(self._settings.llm_max_attempts), 1)
        emitted_any = False

        candidates = deque(
            self._candidates(
                target, messages, eff_model, max_tokens=max_tok, temperature=temp, task=task
            )
        )
        attempts = 0
        last_error: LLMError | None = None
        seen_pairs: set[tuple[str, str]] = set()
        cooling_s = 0.0  # longest wait among candidates skipped by an open breaker

        from app.core.budget import get_current_budget

        req_budget = get_current_budget()
        while candidates and attempts < max_attempts:
            if req_budget is not None and req_budget.is_expired():
                # No time left for this request: another call could only time out and
                # would be charged to a provider that did nothing wrong.
                logger.info("llm_budget_exhausted", attempts=attempts)
                break
            cand_target, cand_model = candidates.popleft()
            pair = (cand_target.provider, cand_model)
            breaker = llm_breakers.get(f"llm:{cand_target.provider}")
            if pair in seen_pairs:
                continue
            if not breaker.allow():
                logger.info(
                    "llm_candidate_skipped", provider=cand_target.provider, model=cand_model
                )
                cooling_s = max(cooling_s, breaker.retry_at - time.monotonic())
                continue
            seen_pairs.add(pair)

            attempts += 1
            provider = cand_target.provider
            started = time.perf_counter()
            first_token = True
            from app.llm.key_validator import get_provider_health_tracker

            tracker = get_provider_health_tracker()
            try:
                async for delta in self._stream_once(
                    cand_target, messages, cand_model, max_tokens=max_tok, temperature=temp
                ):
                    if delta.text:
                        emitted_any = True
                        if first_token:
                            first_token = False
                            ttft = time.perf_counter() - started
                            LLM_TTFT.labels(provider=provider).observe(ttft)
                            STAGE_DURATION.labels(stage="llm_first_token").observe(ttft)
                    yield delta
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                breaker.record(Outcome.OK)
                tracker.record_call_outcome(
                    provider, ok=True, latency_ms=elapsed_ms, status_code=200
                )
                LLM_REQUESTS.labels(provider=provider, outcome="ok").inc()
                STAGE_DURATION.labels(stage="llm_total").observe(elapsed_ms / 1000.0)
                return
            except LLMStatusError as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                status_outcome = (
                    Outcome.RATE_LIMITED if exc.status_code == 429 else Outcome.UPSTREAM_ERROR
                )
                breaker.record(status_outcome, retry_after_s=exc.retry_after_s)
                tracker.record_call_outcome(
                    provider,
                    ok=False,
                    latency_ms=elapsed_ms,
                    status_code=exc.status_code,
                    error=f"http_{exc.status_code}",
                )
                LLM_REQUESTS.labels(provider=provider, outcome=status_outcome.value).inc()
                last_error = exc
                if not exc.retryable:
                    # 400/401/403/404: never retry the same provider; however on
                    # 401/403 with a distinct healthy fallback provider still in
                    # the combo and zero tokens emitted, strip same-provider
                    # entries and fail over cleanly to the next provider.
                    if (
                        exc.status_code in (401, 403)
                        and not emitted_any
                        and any(c_tgt.provider != provider for c_tgt, _ in candidates)
                    ):
                        candidates = deque(
                            (c_tgt, c_mod)
                            for c_tgt, c_mod in candidates
                            if c_tgt.provider != provider
                        )
                        logger.warning(
                            "llm_auth_failover",
                            failed_provider=provider,
                            status_code=exc.status_code,
                            remaining=len(candidates),
                        )
                        continue
                    logger.warning(
                        "llm_non_retryable",
                        provider=exc.provider,
                        status_code=exc.status_code,
                        attempts=attempts,
                    )
                    if emitted_any:
                        raise
                    raise exc from None
                logger.warning(
                    "llm_retryable_error",
                    provider=exc.provider,
                    status_code=exc.status_code,
                    attempts=attempts,
                    retry_after_s=exc.retry_after_s,
                )
                if emitted_any:
                    # Partial answer already streamed — failover would append a
                    # second answer on top. Surface the error instead.
                    raise
                # 429: the same candidate may recover after Retry-After —
                # re-queue it (still bounded by the global attempt budget).
                if exc.retry_after_s is not None:
                    seen_pairs.discard(pair)  # allow the same pair again
                    candidates.appendleft((cand_target, cand_model))
                if candidates and attempts < max_attempts:
                    # docs/29 P3-1 (audit B#13): exponential growth with
                    # uniform jitter so a fleet of 429s does not wake in
                    # lockstep; Retry-After stays the floor.
                    backoff = 0.5 * (2**attempts)
                    jitter = random.uniform(0.8, 1.2)  # nosec B311 - backoff timing, not crypto
                    sleep_s = min(exc.retry_after_s or (backoff * jitter), 30.0)
                    if req_budget is not None:
                        sleep_s = min(sleep_s, max(0.0, req_budget.remaining()))
                    if sleep_s > 0:
                        await asyncio.sleep(sleep_s)
            except LLMError as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                if isinstance(exc, LLMTimeoutError):
                    outcome = Outcome.TIMEOUT if exc.provider_fault else Outcome.CANCELLED
                else:
                    outcome = Outcome.UPSTREAM_ERROR
                breaker.record(outcome)
                tracker.record_call_outcome(
                    provider,
                    ok=False,
                    latency_ms=elapsed_ms,
                    error=type(exc).__name__,
                )
                LLM_REQUESTS.labels(provider=provider, outcome=outcome.value).inc()
                last_error = exc
                logger.warning(
                    "llm_candidate_failed",
                    provider=cand_target.provider,
                    attempts=attempts,
                    error=str(exc)[:120],
                )
                if emitted_any:
                    raise
                continue

        if attempts == 0 and last_error is None and cooling_s > 0:
            raise LLMUnavailableError(
                "every LLM provider is cooling down after repeated failures",
                retry_after_s=max(1.0, cooling_s),
            )
        if emitted_any and last_error is not None:
            raise last_error
        raise last_error or LLMError("LLM call failed: attempt budget exhausted")

    async def _stream_once(
        self,
        target: ResolvedLLMTarget,
        messages: list[dict[str, str]],
        model: str,
        *,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[StreamDelta]:
        if not target.base_url:
            raise LLMError(f"Base URL is not configured for LLM provider '{target.provider}'")
        if not target.api_key and target.provider not in ("pollinations", "ollama", "custom"):
            raise LLMError(
                f"API key for provider '{target.provider}' is not set "
                f"(configure via .env or PATCH /v1/llm/config)"
            )
        url = self._url_for(target)
        headers = self._headers_for(target)
        body = self._body_for(
            target, messages, model, max_tokens=max_tokens, temperature=temperature
        )
        client = self._client or get_http_client("llm")
        cancelled = False  # FIX1: tracks client disconnect for breaker
        timeout_s = self._settings.request_timeout_s
        from app.core.budget import get_current_budget

        budget = get_current_budget()
        if budget is not None:
            timeout_s = max(0.05, min(timeout_s, budget.remaining()))
        # A timeout shorter than this is our deadline, not the provider's failure.
        provider_fault = timeout_s >= min(
            self._settings.request_timeout_s, self._settings.llm_breaker_min_timeout_s
        )

        try:
            async with client.stream(
                "POST",
                url,
                json=body,
                headers=headers,
                timeout=httpx.Timeout(timeout_s, pool=POOL_ACQUIRE_TIMEOUT_S),
            ) as resp:
                if resp.status_code >= 400:
                    detail = await _read_limited(resp)
                    logger.warning(
                        "llm_upstream_error",
                        provider=target.provider,
                        status_code=resp.status_code,
                        detail=detail[:500],
                    )
                    retry_after = resp.headers.get("retry-after")
                    retry_after_s: float | None = None
                    if retry_after:
                        try:
                            retry_after_s = float(retry_after)
                        except (ValueError, TypeError):
                            retry_after_s = None
                    raise LLMStatusError(
                        target.provider, resp.status_code, retry_after_s=retry_after_s
                    )
                content_type = resp.headers.get("content-type", "").lower()
                # Support non-streaming JSON responses from lightweight OpenAI-compatible proxies
                if "application/json" in content_type and "event-stream" not in content_type:
                    raw = await _read_limited(resp)
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        # REL-10: invalid JSON upstream must be an LLMError, not a crash.
                        raise LLMError(f"{target.provider} returned malformed JSON: {exc}") from exc
                    if not isinstance(payload, dict):
                        raise LLMError(f"{target.provider} returned non-object JSON payload")
                    choices = payload.get("choices") or []
                    first = choices[0] if isinstance(choices, list) and choices else None
                    if isinstance(first, dict):
                        msg = first.get("message") or first.get("delta") or {}
                        content = msg.get("content") if isinstance(msg, dict) else None
                        if content:
                            yield StreamDelta(
                                text=str(content),
                                finish_reason=first.get("finish_reason"),
                            )
                    u = payload.get("usage")
                    if isinstance(u, dict):
                        yield StreamDelta(
                            usage=Usage(
                                input_tokens=int(u.get("prompt_tokens") or 0),
                                output_tokens=int(u.get("completion_tokens") or 0),
                            )
                        )
                    return

                async for payload in _sse_payloads(resp.aiter_lines()):
                    delta, usage, finish = _parse_delta(payload, target.protocol)
                    if delta is not None or usage is not None or finish is not None:
                        yield StreamDelta(text=delta, usage=usage, finish_reason=finish)
        except (GeneratorExit, asyncio.CancelledError):
            # FIX1: client disconnected OR task cancelled mid-attempt. Record
            # CANCELLED via the finally block so the half-open breaker trial
            # doesn't stay stuck in_flight=True.
            cancelled = True
            raise
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise LLMTimeoutError(
                f"{target.provider} timeout: {type(exc).__name__}", provider_fault=provider_fault
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"{target.provider} transport error: {type(exc).__name__}") from exc
        finally:
            if cancelled:
                llm_breakers.get(f"llm:{target.provider}").record(Outcome.CANCELLED)

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        task: str = "intent",
    ) -> tuple[str, Usage]:
        """Non-streaming completion for the research agents (planner/extractor).

        RET-10 budget: retries live inside :meth:`stream` (bounded by
        ``llm_max_attempts``); this wrapper makes exactly one bounded call
        instead of multiplying the budget (old code: 3 x up-to-21 calls).
        """
        target = self.resolve_target()
        eff_model = self._effective_model(model, target, task=task)
        text_parts: list[str] = []
        usage = Usage()
        try:
            async for delta in self.stream(
                messages, model=eff_model, max_tokens=max_tokens, task=task
            ):
                if delta.text:
                    text_parts.append(delta.text)
                if delta.usage:
                    usage.input_tokens += delta.usage.input_tokens
                    usage.output_tokens += delta.usage.output_tokens
            output = "".join(text_parts)
            self._schedule_trace_complete(task, messages, eff_model, output, usage)
            return output, usage
        except LLMError:
            # complete() has no emitted stream to protect; one bounded pass is
            # the whole budget. Re-raise for the caller to degrade gracefully.
            raise

    def _schedule_trace_complete(
        self, task: str, messages: list[dict[str, str]], model: str, output: str, usage: Usage
    ) -> None:
        from app.core.llm_tracing import langfuse_enabled

        if not langfuse_enabled(self._settings):
            return
        t = asyncio.create_task(self._trace_complete(task, messages, model, output, usage))
        _trace_tasks.add(t)
        t.add_done_callback(_trace_tasks.discard)

    async def _trace_complete(
        self, task: str, messages: list[dict[str, str]], model: str, output: str, usage: Usage
    ) -> None:
        from app.core.llm_tracing import trace_llm_call

        await trace_llm_call(
            self._settings,
            task=task,
            model=model,
            messages=messages,
            output=output,
            usage={
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            },
        )


def _parse_delta(
    payload: dict[str, Any], protocol: str
) -> tuple[str | None, Usage | None, str | None]:
    """Extract (text, usage, finish_reason) from one SSE payload; any may be absent."""
    if protocol == "anthropic":
        return _parse_anthropic(payload)
    return _parse_openai(payload)


def _parse_openai(payload: dict[str, Any]) -> tuple[str | None, Usage | None, str | None]:
    usage = None
    if payload.get("usage"):
        u = payload["usage"]
        usage = Usage(
            input_tokens=int(u.get("prompt_tokens") or 0),
            output_tokens=int(u.get("completion_tokens") or 0),
        )
    choices = payload.get("choices") or []
    text = None
    finish = None
    if choices:
        delta = choices[0].get("delta") or {}
        text = delta.get("content")
        finish = choices[0].get("finish_reason")
    return text, usage, finish


def _parse_anthropic(payload: dict[str, Any]) -> tuple[str | None, Usage | None, str | None]:
    event_type = payload.get("type")
    if event_type == "content_block_delta":
        text = (payload.get("delta") or {}).get("text")
        return text, None, None
    if event_type == "message_start":
        u = (payload.get("message") or {}).get("usage") or {}
        return None, Usage(input_tokens=int(u.get("input_tokens") or 0), output_tokens=0), None
    if event_type == "message_delta":
        u = payload.get("usage") or {}
        stop_reason = payload.get("delta", {}).get("stop_reason")
        return (
            None,
            Usage(input_tokens=0, output_tokens=int(u.get("output_tokens") or 0)),
            stop_reason,
        )
    return None, None, None


def record_usage(model: str, usage: Usage) -> None:
    """Export token + cost metrics for one completion."""
    LLM_TOKENS.labels(model=model, direction="input").inc(usage.input_tokens)
    LLM_TOKENS.labels(model=model, direction="output").inc(usage.output_tokens)
    cost = usage.cost_usd(model)
    if cost > 0:
        LLM_COST.labels(model=model).inc(cost)


_ = time  # referenced via breaker internals; keep import surface stable
