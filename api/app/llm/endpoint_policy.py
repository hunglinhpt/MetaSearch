"""LLM Endpoint Privacy Policy (docs/30 Part 1) — trusted-only allowlist.

Default-deny governance for every LLM base_url this server can dispatch
prompts to: only enterprise-grade providers with zero-data-retention /
non-training guarantees and self-hosted local inference (loopback / LAN
Ollama, vLLM, llama.cpp) are trusted. Unknown third-party endpoints
(openrouter, pollinations, groq mirrors, arbitrary proxies …) are rejected
**before a single prompt byte leaves the server**.

Two enforcement points, both fail-fast:
- runtime: `enforce_llm_endpoint_policy` inside the gateway dispatch path
  (every candidate target, every request);
- startup: `sweep_configured_endpoints` in the app lifespan — a misconfigured
  deployment refuses to boot quietly into a leaking state.

Operators who genuinely need an unlisted endpoint (self-hosted on a LAN name,
an enterprise gateway) set `LLM_ENDPOINT_POLICY=open` — the SSRF validator
(`providers.validate_llm_base_url`) still applies in every mode.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from app.core.errors import ErrorCode, MetaSearchError
from app.core.logging import get_logger

logger = get_logger(__name__)

# Enterprise-grade hosts with zero-data-retention / non-training API terms.
# Suffix-matched (foo.api.openai.com is allowed, api.openai.com.evil.io is not).
TRUSTED_LLM_HOSTS: frozenset[str] = frozenset(
    {
        "api.anthropic.com",
        "api.openai.com",
        "generativelanguage.googleapis.com",
        "aiplatform.googleapis.com",
    }
)

# Self-hosted local inference is always trusted — prompts never leave the box.
_LOCAL_SELF_HOSTED_HOSTS: frozenset[str] = frozenset(
    {  # nosec B104 - hostname allowlist entries for local inference, not bind calls
        "localhost",
        "127.0.0.1",
        "::1",
        "[::1]",
        "0.0.0.0",
        "host.docker.internal",
    }
)

_LAN_SUFFIXES: tuple[str, ...] = (
    ".local",
    ".lan",
    ".internal",
    ".home.arpa",
)

LLM_ENDPOINT_POLICY = "trusted_only"  # process-wide; overridden by settings


def _host_trusted(host: str) -> bool:
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host in _LOCAL_SELF_HOSTED_HOSTS:
        return True
    if host.endswith(_LAN_SUFFIXES):
        return True  # LAN names: Ollama/vLLM/llama.cpp boxes on the local net
    # bare hostnames without dots = LAN (e.g. "ollama-box"); FQDNs must match
    # the trusted enterprise list by suffix.
    if "." not in host:
        return True
    return any(host == th or host.endswith(f".{th}") for th in TRUSTED_LLM_HOSTS)


def llm_endpoint_is_trusted(url: str) -> bool:
    """True when ``url``'s host is an enterprise-grade provider or local/LAN
    self-hosted inference."""
    parsed = urlsplit((url or "").strip())
    return _host_trusted(parsed.hostname or "")


def enforce_llm_endpoint_policy(url: str, *, policy: str = LLM_ENDPOINT_POLICY) -> str:
    """Runtime guard: reject untrusted LLM endpoints before dispatch.

    ``policy`` is the settings value (`llm_endpoint_policy`):
    ``trusted_only`` (default) rejects anything not on the trusted list;
    ``open`` keeps the legacy behavior (the SSRF validator still runs
    separately in providers.validate_llm_base_url).
    """
    cleaned = (url or "").strip()
    if not cleaned or policy != "trusted_only":
        return cleaned
    if llm_endpoint_is_trusted(cleaned):
        return cleaned
    host = urlsplit(cleaned).hostname or "?"
    raise MetaSearchError(
        ErrorCode.INVALID_REQUEST,
        (
            f"LLM endpoint '{host}' is not on the trusted provider allowlist "
            "(enterprise zero-data-retention providers or local inference "
            "only). Set LLM_ENDPOINT_POLICY=open to override at your own risk."
        ),
    )


def sweep_configured_endpoints(
    endpoints: list[str], *, policy: str = LLM_ENDPOINT_POLICY
) -> list[str]:
    """Startup sweep: return the endpoints violating the policy (empty when
    clean). The caller decides to fail-fast or log-and-continue per route."""
    if policy != "trusted_only":
        return []
    return [e for e in endpoints if e and not llm_endpoint_is_trusted(e)]
