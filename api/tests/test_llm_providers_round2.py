"""LLM providers round 2 (docs/25): SSRF base-url validator branches, key
masking, extra-header parsing, runtime override store, model resolution."""

from __future__ import annotations

import socket

import pytest

from app.config import Settings
from app.core.errors import MetaSearchError
from app.llm.providers import (
    LLMOverride,
    RuntimeLLMConfigStore,
    get_runtime_llm_store,
    mask_api_key,
    parse_extra_headers,
    resolve_llm_target,
    validate_llm_base_url,
)

# --- validate_llm_base_url ---------------------------------------------------------


def test_validate_llm_base_url_empty():
    assert validate_llm_base_url("") == ""
    assert validate_llm_base_url("   ") == ""


def test_validate_llm_base_url_rejects_non_http_and_hostless():
    with pytest.raises(MetaSearchError, match="valid http"):
        validate_llm_base_url("ftp://files.example.com")
    with pytest.raises(MetaSearchError, match="valid http"):
        validate_llm_base_url("https://")


def test_validate_llm_base_url_byo_requires_allowed_hosts():
    settings_hosts = ""
    with pytest.raises(MetaSearchError, match="no allowed hosts configured"):
        validate_llm_base_url(
            "https://api.example.com/v1", is_byo=True, allowed_hosts=settings_hosts
        )
    with pytest.raises(MetaSearchError, match="not in allowed hosts"):
        validate_llm_base_url(
            "https://api.example.com/v1",
            is_byo=True,
            allowed_hosts="vendor.example.com",
        )
    ok = validate_llm_base_url(
        "https://api.vendor.example.com/v1",
        is_byo=True,
        allowed_hosts="vendor.example.com",
    )
    assert ok.endswith("/v1")


def test_validate_llm_base_url_allowed_hosts_non_byo():
    with pytest.raises(MetaSearchError, match="not in allowed hosts"):
        validate_llm_base_url("https://api.example.com/v1", allowed_hosts="only.example.com")
    # subdomain suffix match is accepted
    ok = validate_llm_base_url("https://api.only.example.com/v1", allowed_hosts="only.example.com")
    assert ok


def test_validate_llm_base_url_private_allowlist_and_metadata_and_wildcard():
    # explicit private allowlist (admin-configured, e.g. ollama daemon)
    ok = validate_llm_base_url(
        "http://localhost:11434/v1", allow_private=True, private_hosts="127.0.0.1,localhost"
    )
    assert ok.startswith("http://localhost")
    # metadata endpoints always blocked
    with pytest.raises(MetaSearchError, match="blocked for security"):
        validate_llm_base_url("http://169.254.169.254/latest")
    with pytest.raises(MetaSearchError, match="blocked for security"):
        validate_llm_base_url("https://metadata.google.internal/computeMetadata")
    # wildcard DNS services blocked
    with pytest.raises(MetaSearchError, match="wildcard DNS"):
        validate_llm_base_url("https://myapp.nip.io/v1")
    # single-label internal hostname blocked
    with pytest.raises(MetaSearchError, match="internal network name"):
        validate_llm_base_url("http://searxng:8080")


def test_validate_llm_base_url_ip_literals(monkeypatch):
    # private/reserved IP literal rejected without DNS
    with pytest.raises(MetaSearchError, match="forbidden private/reserved"):
        validate_llm_base_url("http://10.0.0.5:8000/v1")
    # loopback rejected (allow_private covers named hosts, not bare IPs)
    with pytest.raises(MetaSearchError, match="forbidden private/reserved"):
        validate_llm_base_url("http://127.0.0.1:8080/v1")
    # public IP literal accepted
    assert validate_llm_base_url("http://93.184.216.34/v1").endswith("/v1")


def test_validate_llm_base_url_dns_resolution(monkeypatch):
    # host resolving to a private IP is rejected
    def private_dns(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", private_dns)
    with pytest.raises(MetaSearchError, match="resolves to forbidden private IP"):
        validate_llm_base_url("https://intranet.example.com/v1")

    def broken_dns(host, port, *a, **k):
        raise socket.gaierror("nameserver gone")

    monkeypatch.setattr(socket, "getaddrinfo", broken_dns)
    with pytest.raises(MetaSearchError, match="Failed to resolve"):
        validate_llm_base_url("https://dead.example.com/v1")


# --- key masking -------------------------------------------------------------------


def test_mask_api_key_variants():
    assert mask_api_key("") == ""
    assert mask_api_key("   ") == ""
    assert mask_api_key("short") == "***"
    assert mask_api_key("1234567890123") == "12...23"  # < 16 chars
    assert mask_api_key("sk-abcdefghij123456") == "sk-a...3456"


# --- extra headers -----------------------------------------------------------------


def test_parse_extra_headers_variants():
    assert parse_extra_headers(None) == {}
    assert parse_extra_headers({}) == {}
    assert parse_extra_headers({"X-A": "1"}) == {"X-A": "1"}
    assert parse_extra_headers('{"X-B": "2"}') == {"X-B": "2"}
    assert parse_extra_headers("   ") == {}
    assert parse_extra_headers("not-json{") == {}
    assert parse_extra_headers('["not-a-dict"]') == {}
    # hop-by-hop headers stripped regardless of case
    out = parse_extra_headers({"host": "evil", "Connection": "close", "X-Ok": "yes"})
    assert out == {"X-Ok": "yes"}


# --- runtime override store --------------------------------------------------------


def test_runtime_store_update_merges_and_validates():
    store = RuntimeLLMConfigStore()
    first = store.update(LLMOverride(provider="groq", api_key="k1"))
    assert first.api_key == "k1"
    second = store.update(LLMOverride(api_key="k2", model="m1"))
    assert second.api_key == "k2" and second.model == "m1"
    assert second.provider == "groq"  # merged, not replaced
    # base_url validated on set
    with pytest.raises(MetaSearchError):
        store.update(LLMOverride(base_url="http://169.254.169.254"))
    store.reset()
    assert store.get_override() is None
    assert store.get_council_endpoints() is None
    assert get_runtime_llm_store() is not None


def test_runtime_store_council_endpoints_validated():
    store = RuntimeLLMConfigStore()
    with pytest.raises(MetaSearchError):
        store.set_council_endpoints([LLMOverride(base_url="http://169.254.169.254")])
    good = [LLMOverride(provider="groq", base_url="https://api.groq.com/openai/v1")]
    store.set_council_endpoints(good)
    assert store.get_council_endpoints()[0].base_url.startswith("https://api.groq.com")
    store.set_council_endpoints(None)
    assert store.get_council_endpoints() is None


# --- resolve_llm_target model/headers branches -------------------------------------


def test_resolve_target_prefers_preset_models_for_non_openai():
    settings = Settings(ENVIRONMENT="test", LLM_PROVIDER="groq")
    target = resolve_llm_target(settings)
    assert target.provider == "groq"
    assert target.quick_model != "gpt-4o-mini"  # preset default, not OpenAI stock
    assert target.protocol in ("openai", "anthropic")


def test_resolve_target_explicit_models_override():
    settings = Settings(ENVIRONMENT="test", LLM_PROVIDER="groq")
    target = resolve_llm_target(
        settings, request_override=LLMOverride(model="my-model", quick_model="quick-x")
    )
    assert target.quick_model == "my-model"  # model wins over quick_model
    assert target.report_model == "my-model"


def test_resolve_target_openrouter_fallbacks_and_headers():
    settings = Settings(
        ENVIRONMENT="test",
        LLM_PROVIDER="openrouter",
        OPENROUTER_SITE_URL="https://metasearch.ai",
        OPENROUTER_APP_NAME="MetaSearch",
        OPENROUTER_FALLBACK_MODELS="m1, m2 ,",
    )
    target = resolve_llm_target(settings)
    assert target.extra_headers["HTTP-Referer"] == "https://metasearch.ai"
    assert target.extra_headers["X-Title"] == "MetaSearch"
    assert target.fallback_models == ["m1", "m2"]


def test_resolve_target_invalid_fallback_provider_raises():
    settings = Settings(ENVIRONMENT="test", LLM_PROVIDER="groq")
    with pytest.raises(MetaSearchError, match="Invalid fallback_provider"):
        resolve_llm_target(settings, request_override=LLMOverride(fallback_provider="nope"))


def test_resolve_target_custom_env_headers_trusted_override_headers_stripped():
    settings = Settings(
        ENVIRONMENT="test",
        LLM_PROVIDER="custom",
        CUSTOM_LLM_BASE_URL="https://api.myllm.example.com/v1",
        CUSTOM_LLM_HEADERS='{"X-Team": "core", "Authorization": "Bearer operator-set"}',
        llm_byo_enabled=True,
    )
    # env-configured headers are operator-trusted: Authorization passes through
    target = resolve_llm_target(settings)
    assert target.extra_headers.get("X-Team") == "core"
    assert target.extra_headers.get("Authorization") == "Bearer operator-set"
    # an OVERRIDE that sets a fresh auth header is stripped when no request
    # key accompanies it (cross-key leakage guard on the override itself)
    settings2 = Settings(
        ENVIRONMENT="test",
        LLM_PROVIDER="custom",
        CUSTOM_LLM_BASE_URL="https://api.myllm.example.com/v1",
        CUSTOM_LLM_HEADERS='{"X-Team": "core"}',
        llm_byo_enabled=True,
    )
    target2 = resolve_llm_target(
        settings2, request_override=LLMOverride(extra_headers={"Authorization": "Bearer leak"})
    )
    assert "Authorization" not in target2.extra_headers
    # with a request-provided key the caller may pass auth headers through
    target3 = resolve_llm_target(
        settings2,
        request_override=LLMOverride(
            api_key="k" * 12, extra_headers={"Authorization": "Bearer ok"}
        ),
    )
    assert target3.extra_headers.get("Authorization") == "Bearer ok"


def test_resolve_target_runtime_key_used_for_env_base_url():
    settings = Settings(ENVIRONMENT="test", LLM_PROVIDER="groq")
    target = resolve_llm_target(
        settings, runtime_override=LLMOverride(api_key="runtime-key-123456")
    )
    assert target.api_key == "runtime-key-123456"
