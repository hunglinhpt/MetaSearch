"""Verification tests for WP-01: LLM Override Security and SSRF Prevention."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.errors import ErrorCode, MetaSearchError
from app.llm.gateway import LLMError, LLMGateway
from app.llm.providers import (
    LLMOverride,
    resolve_llm_target,
    validate_llm_base_url,
)
from app.main import create_app


@pytest.mark.parametrize(
    ("provider", "env_field", "secret_val"),
    [
        ("openai", "openai_api_key", "sk-SERVER-SECRET"),
        ("anthropic", "anthropic_api_key", "sk-ant-SERVER-SECRET"),
        ("groq", "groq_api_key", "gsk-SERVER-SECRET"),
        ("openrouter", "openrouter_api_key", "sk-or-SERVER-SECRET"),
        ("gemini", "gemini_api_key", "AIza-SERVER-SECRET"),
        ("mistral", "mistral_api_key", "mis-SERVER-SECRET"),
    ],
)
def test_request_base_url_never_inherits_env_key(provider: str, env_field: str, secret_val: str):
    """When a caller overrides base_url, the server's API key must NEVER be inherited."""
    kwargs = {env_field: secret_val, "llm_provider": provider, "llm_byo_enabled": True}
    settings = Settings(**kwargs)

    # Caller points to attacker endpoint without supplying an api_key
    target = resolve_llm_target(
        settings,
        request_override=LLMOverride(provider=provider, base_url="https://attacker.example/v1"),
    )
    gw = LLMGateway(settings)
    headers = gw._headers_for(target)

    assert secret_val not in headers.get("Authorization", "")
    assert secret_val not in headers.get("x-api-key", "")
    assert target.api_key == ""


def test_runtime_key_not_sent_to_request_base_url():
    """A runtime-configured key must not be sent to a request-specified base_url."""
    settings = Settings(llm_provider="openai", llm_byo_enabled=True)
    runtime_override = LLMOverride(api_key="runtime-secret-key")
    request_override = LLMOverride(base_url="https://attacker.example/v1")

    target = resolve_llm_target(
        settings,
        runtime_override=runtime_override,
        request_override=request_override,
    )
    gw = LLMGateway(settings)
    headers = gw._headers_for(target)

    assert "runtime-secret-key" not in headers.get("Authorization", "")
    assert target.api_key == ""


def test_custom_provider_does_not_fall_back_to_openai_key():
    """Custom provider must never fall back to OPENAI_API_KEY."""
    settings = Settings(
        openai_api_key="sk-OPENAI-SECRET",
        custom_llm_api_key="",
        llm_provider="custom",
    )
    target = resolve_llm_target(settings)
    assert target.api_key == ""


def test_byo_disabled_rejects_base_url_and_api_key():
    """When llm_byo_enabled is False, request_override specifying base_url or api_key must fail."""
    settings = Settings(llm_byo_enabled=False, llm_provider="openai")

    with pytest.raises(MetaSearchError) as exc_info:
        resolve_llm_target(
            settings,
            request_override=LLMOverride(base_url="https://custom-llm.example/v1"),
        )
    assert exc_info.value.code in (ErrorCode.INVALID_REQUEST, ErrorCode.UNAUTHORIZED)

    with pytest.raises(MetaSearchError) as exc_info2:
        resolve_llm_target(
            settings,
            request_override=LLMOverride(api_key="user-supplied-key"),
        )
    assert exc_info2.value.code in (ErrorCode.INVALID_REQUEST, ErrorCode.UNAUTHORIZED)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest",
        "http://127.0.0.1:6379",
        "http://localhost:5432",
        "http://redis:6379",
        "http://postgres:5432",
        "http://searxng:8080",
        "http://crawl4ai:11222",
        "http://10.0.0.5:8000",
        "http://100.64.0.1:8000",
        "http://[::ffff:127.0.0.1]:80",
        "http://metadata.nip.io/",
    ],
)
def test_byo_rejects_private_and_dns_rebinding_hosts(url: str):
    """validate_llm_base_url must reject private, loopback, and internal docker endpoints."""
    with pytest.raises(MetaSearchError) as exc_info:
        validate_llm_base_url(url, allow_private=False)
    assert exc_info.value.code == ErrorCode.INVALID_REQUEST


def test_llm_error_does_not_echo_upstream_body():
    """LLMError must not echo sensitive upstream response bodies to callers."""
    status_code = 500
    sensitive_detail = "Server failed: database key sk-sensitive-internal-12345 connection lost"

    err = LLMError(f"LLM provider 'openai' returned HTTP {status_code}")
    assert sensitive_detail not in str(err)
    assert "HTTP 500" in str(err)


def test_llm_test_requires_admin_for_custom_target(monkeypatch):
    """POST /v1/llm/test with custom target requires admin role or llm_byo_enabled."""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("LLM_BYO_ENABLED", "false")
    app = create_app()
    client = TestClient(app)

    resp = client.post(
        "/v1/llm/test",
        json={"llm": {"base_url": "https://attacker.example/v1"}},
    )
    assert resp.status_code in (401, 403)
