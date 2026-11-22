"""LLM key validator coverage round 2 (docs/25): offline format checks,
URL/header sanitization, and live probe branches via MockTransport."""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.llm.key_validator import (
    ProviderHealthTracker,
    _extract_models_count,
    _is_placeholder_key,
    _parse_retry_after,
    _probe_chat_url,
    _probe_headers,
    _probe_models_url,
    _sanitize_base_url_for_display,
    scoped_runtime_override,
    validate_all_configured_providers,
    validate_key_format,
    validate_provider_key,
)
from app.llm.providers import LLMOverride, ResolvedLLMTarget


@pytest.fixture(autouse=True)
def _clear_llm_breakers():
    """Live probes record failures into the shared gateway breaker singleton;
    clear it around every test so later gateway tests start from a clean slate."""
    from app.llm.gateway import llm_breakers

    llm_breakers._breakers.clear()
    yield
    llm_breakers._breakers.clear()


def _mock(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- offline format validation ----------------------------------------------------


def test_validate_key_format_unknown_provider():
    res = validate_key_format("not-a-provider", "k")
    assert res.reason == "unknown_provider" and res.configured and res.format_valid


def test_validate_key_format_missing_key():
    res = validate_key_format("openai", "")
    assert res.configured is False and res.format_valid is False
    assert res.reason == "missing_api_key" and res.masked_key == ""


def test_validate_key_format_placeholder_markers():
    assert _is_placeholder_key("your-api-key-here") == "placeholder_marker:your-api-key"
    assert _is_placeholder_key("sk- xxxx") == "contains_whitespace"
    assert _is_placeholder_key("kéy-vâlue-12345") == "non_ascii_characters"
    assert _is_placeholder_key("aaaaaaaaaa") == "low_entropy_repeated_chars"
    assert _is_placeholder_key("sk-abc123xyz9876") is None


def test_validate_key_format_length_and_prefix_bounds():
    # prefix enforced for known specs (openai = sk-...)
    res = validate_key_format("openai", "bad-prefix-" + "x" * 40)
    assert res.format_valid is False
    assert "expected_prefix" in res.reason
    # too short (distinct chars >= 6 so the low-entropy guard does not fire first)
    res_short = validate_key_format("openai", "sk-abcdef12345")
    assert res_short.reason.startswith("too_short_min_")
    # too long
    res_long = validate_key_format("openai", "sk-" + "abcdefghij" * 60)  # 603 > 512
    assert res_long.reason.startswith("too_long_max_")
    # happy path
    ok = validate_key_format("openai", "sk-abcdef1234567890abcdef1234567890ab")
    assert ok.format_valid is True and ok.reason == "ok"


def test_validate_key_format_keyless_or_local():
    res = validate_key_format("ollama", "")
    assert res.reason == "keyless_or_local" and res.format_valid is True


# --- URL / header / payload helpers -----------------------------------------------


def test_sanitize_base_url_strips_userinfo_query_and_fragment():
    out = _sanitize_base_url_for_display("https://user:secret@api.example.com/v1/?x=1#frag")
    assert "secret" not in out and "x=1" not in out and "frag" not in out
    assert out.endswith("/v1")
    assert _sanitize_base_url_for_display("") == ""
    assert _sanitize_base_url_for_display("not a url??") != ""  # fallback strips query


def test_probe_headers_by_protocol():
    anthropic = ResolvedLLMTarget(
        provider="anthropic",
        protocol="anthropic",
        base_url="https://api.anthropic.com",
        api_key="ak-1",
        quick_model="m",
        report_model="m",
        extra_headers={},
        fallback_models=[],
        fallback_provider="",
    )
    headers = _probe_headers(anthropic)
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["x-api-key"] == "ak-1"
    openai_like = ResolvedLLMTarget(
        provider="openai",
        protocol="openai",
        base_url="https://api.openai.com/v1",
        api_key="sk-1",
        quick_model="m",
        report_model="m",
        extra_headers={"X-OK": "1", "X-Bad": "bad\rvalue"},
        fallback_models=[],
        fallback_provider="",
    )
    headers2 = _probe_headers(openai_like)
    assert headers2["Authorization"] == "Bearer sk-1"
    assert "X-Bad" not in headers2  # CRLF-carrying header dropped
    assert headers2["X-OK"] == "1"


def test_probe_urls_anthropic_and_openrouter():
    def target(provider: str, protocol: str, base_url: str) -> ResolvedLLMTarget:
        return ResolvedLLMTarget(
            provider=provider,
            protocol=protocol,
            base_url=base_url,
            api_key="k",
            quick_model="m",
            report_model="m",
            extra_headers={},
            fallback_models=[],
            fallback_provider="",
        )

    t = target("anthropic", "anthropic", "https://api.anthropic.com/v1")
    assert _probe_models_url(t) == "https://api.anthropic.com/v1/models"
    assert _probe_chat_url(t) == "https://api.anthropic.com/v1/messages"
    t2 = target("anthropic", "anthropic", "https://api.anthropic.com")
    assert _probe_models_url(t2) == "https://api.anthropic.com/v1/models"
    t3 = target("openrouter", "openai", "https://openrouter.ai/api/v1")
    assert _probe_models_url(t3) == "https://openrouter.ai/api/v1/auth/key"
    t4 = target("openai", "openai", "https://api.openai.com/v1/")
    assert _probe_chat_url(t4) == "https://api.openai.com/v1/chat/completions"


def test_extract_models_count_shapes():
    assert _extract_models_count(b'{"data": [1, 2, 3]}') == 3
    assert _extract_models_count(b'{"models": [1]}') == 1
    assert _extract_models_count(b"[1, 2]") == 2
    assert _extract_models_count(b'{"data": "nope"}') is None
    assert _extract_models_count(b"not json") is None


def test_parse_retry_after():
    resp = httpx.Response(429, headers={"Retry-After": "12.5"})
    assert _parse_retry_after(resp) == 12.5
    assert _parse_retry_after(httpx.Response(429, headers={"Retry-After": "soon"})) is None
    assert _parse_retry_after(httpx.Response(429)) is None


def test_scoped_runtime_override_provider_gating():
    settings = Settings(ENVIRONMENT="test", LLM_PROVIDER="openai")
    override = LLMOverride(provider="groq", api_key="k")
    assert scoped_runtime_override(override, "openai", settings) is None
    assert scoped_runtime_override(override, "groq", settings) is override
    assert scoped_runtime_override(None, "openai", settings) is None


def test_health_tracker_records_and_resets():
    tracker = ProviderHealthTracker()
    state0 = tracker.get("groq")
    assert state0.last_probe_ok is None  # get() auto-creates a fresh state
    tracker.reset()
    state1 = tracker.get("groq")
    assert state1 is not state0


# --- live probe branches (MockTransport) ------------------------------------------

BYO_KW = {"llm_byo_enabled": True}


@pytest.mark.asyncio
async def test_validate_provider_key_disabled_provider():
    settings = Settings(ENVIRONMENT="test", LLM_PROVIDER="none")
    res = await validate_provider_key("none", settings)
    assert res.key_valid is None and res.reachable is None
    assert res.error == "provider_disabled"


@pytest.mark.asyncio
async def test_validate_provider_key_dry_run_skips_probe():
    settings = Settings(ENVIRONMENT="test", **BYO_KW)
    res = await validate_provider_key(
        "openai",
        settings,
        request_override=LLMOverride(api_key="sk-" + "abcdefghij" * 4),
        dry_run=True,
    )
    assert res.format_valid is True
    assert res.key_valid is None  # no probe performed
    assert res.reachable is None


@pytest.mark.asyncio
async def test_validate_provider_key_probe_200_with_models_count():
    settings = Settings(ENVIRONMENT="test", **BYO_KW)
    key = "sk-" + "abcdefghij" * 4

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        assert request.headers["Authorization"] == f"Bearer {key}"
        return httpx.Response(200, json={"data": [{"id": "gpt"}] * 5})

    res = await validate_provider_key(
        "openai",
        settings,
        request_override=LLMOverride(api_key=key),
        client=_mock(handler),
        timeout_s=2.0,
    )
    assert res.reachable is True and res.key_valid is True
    assert res.models_count == 5
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_validate_provider_key_probe_401():
    settings = Settings(ENVIRONMENT="test", **BYO_KW)
    key = "sk-" + "bcdefghijk" * 4
    res = await validate_provider_key(
        "openai",
        settings,
        request_override=LLMOverride(api_key=key),
        client=_mock(lambda r: httpx.Response(401, text="no")),
        timeout_s=2.0,
    )
    assert res.reachable is True and res.key_valid is False
    assert "authentication_failed" in res.error


@pytest.mark.asyncio
async def test_validate_provider_key_probe_429_rate_limited():
    settings = Settings(ENVIRONMENT="test", **BYO_KW)
    key = "sk-" + "cdefghijkl" * 4
    res = await validate_provider_key(
        "openai",
        settings,
        request_override=LLMOverride(api_key=key),
        client=_mock(lambda r: httpx.Response(429, headers={"Retry-After": "7"})),
        timeout_s=2.0,
    )
    assert res.rate_limited is True and res.key_valid is True
    assert res.error == "rate_limited (HTTP 429)"


@pytest.mark.asyncio
async def test_validate_provider_key_probe_404_falls_back_to_chat():
    """pollinations/github_models auto-fallback: 404 on /models -> 1-token chat probe."""
    settings = Settings(ENVIRONMENT="test", **BYO_KW)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/models"):
            return httpx.Response(404)
        assert request.url.path.endswith("/chat/completions")
        body = request.read()
        assert b'"max_tokens":1' in body
        return httpx.Response(200, json={"choices": []})

    res = await validate_provider_key(
        "pollinations",
        settings,
        client=_mock(handler),
        timeout_s=2.0,
    )
    assert res.reachable is True and res.key_valid is True
    assert any(p.endswith("/chat/completions") for p in calls)


@pytest.mark.asyncio
async def test_validate_provider_key_probe_transport_error():
    settings = Settings(ENVIRONMENT="test", **BYO_KW)
    key = "sk-" + "defghijklm" * 4

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    res = await validate_provider_key(
        "openai",
        settings,
        request_override=LLMOverride(api_key=key),
        client=_mock(handler),
        timeout_s=2.0,
    )
    assert res.reachable is False and res.key_valid is None
    assert res.error == "ConnectError"


@pytest.mark.asyncio
async def test_validate_provider_key_custom_missing_base_url():
    settings = Settings(ENVIRONMENT="test")
    res = await validate_provider_key("custom", settings)
    assert res.format_valid is False
    assert res.format_reason == "missing_base_url"


@pytest.mark.asyncio
async def test_validate_provider_key_ssrf_blocked_base_url():
    settings = Settings(ENVIRONMENT="test")
    res = await validate_provider_key(
        "custom",
        settings,
        request_override=LLMOverride(
            provider="custom", base_url="http://127.0.0.1:8080/v1", api_key="k" * 12
        ),
        timeout_s=2.0,
    )
    assert res.reachable is False and res.key_valid is False
    assert res.format_reason == "target_resolution_rejected" or res.error


@pytest.mark.asyncio
async def test_validate_all_configured_providers_skips_unknown_ids():
    settings = Settings(ENVIRONMENT="test")
    res = await validate_all_configured_providers(
        settings, providers=["not-a-provider", "none"], client=_mock(lambda r: httpx.Response(200))
    )
    assert res == []
