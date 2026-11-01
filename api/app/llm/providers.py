"""Registry of LLM provider presets (OpenRouter Free, Pollinations, Gemini, Groq, etc.),
runtime hot-configuration store, and BYO-LLM override models.

Supports two complementary integration modes:
1. Built-in / BYO-LLM Gateway: MetaSearch calls OpenRouter Free (``openrouter/free``),
   other reputable free tiers (Pollinations keyless, Google Gemini Free, Groq LPU Free,
   Cerebras Free, Mistral Free, GitHub Models, HuggingFace Router, local Ollama/vLLM),
   or an external system's internal LLM proxy (LiteLLM, vLLM, Kong, Azure, custom URL).
2. External LLM Orchestrator Mode: External systems with their own LLM call
   ``POST /v1/context`` to obtain deduplicated, scraped, forensic-enriched, reranked
   context chunks + ready-to-send prompt messages, then validate their LLM's citations
   via ``POST /v1/citations/validate``.
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from app.config import Settings
from app.core.errors import ErrorCode, MetaSearchError

ProviderId = Literal[
    "openai",
    "anthropic",
    "openrouter",
    "pollinations",
    "gemini",
    "groq",
    "cerebras",
    "mistral",
    "github_models",
    "huggingface",
    "ollama",
    "custom",
    "none",
]

ProtocolType = Literal["openai", "anthropic", "none"]

_BLOCKED_METADATA_HOSTS = frozenset(
    {
        "169.254.169.254",
        "metadata.google.internal",
        "metadata.goog",
        "100.100.100.200",
        "fd00:ec2::254",
    }
)


@dataclass(frozen=True, slots=True)
class LLMProviderPreset:
    id: ProviderId
    name: str
    protocol: ProtocolType
    base_url: str
    default_quick_model: str
    default_report_model: str
    is_free: bool
    requires_api_key: bool
    env_key_name: str
    signup_url: str
    docs_url: str
    description: str
    recommended_models: list[str] = field(default_factory=list)


LLM_PROVIDER_PRESETS: dict[str, LLMProviderPreset] = {
    "openrouter": LLMProviderPreset(
        id="openrouter",
        name="OpenRouter Free Router (openrouter/free)",
        protocol="openai",
        base_url="https://openrouter.ai/api/v1",
        default_quick_model="openrouter/free",
        default_report_model="openrouter/free",
        is_free=True,
        requires_api_key=True,
        env_key_name="OPENROUTER_API_KEY",
        signup_url="https://openrouter.ai/openrouter/free",
        docs_url="https://openrouter.ai/docs/quickstart",
        description=(
            "Official OpenRouter Free Models Router ($0/M input & output). Automatically routes "
            "to top available free models (Llama 3.3 70B, DeepSeek V3/R1, Gemini 2.0 Flash, "
            "Qwen 2.5 72B, Gemma 3 27B) with built-in fallback."
        ),
        recommended_models=[
            "openrouter/free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "deepseek/deepseek-chat-v3-0324:free",
            "deepseek/deepseek-r1:free",
            "google/gemini-2.0-flash-exp:free",
            "qwen/qwen-2.5-72b-instruct:free",
            "google/gemma-3-27b-it:free",
            "mistralai/mistral-small-3.1-24b-instruct:free",
        ],
    ),
    "pollinations": LLMProviderPreset(
        id="pollinations",
        name="Pollinations AI (Keyless Zero-Config Free)",
        protocol="openai",
        base_url="https://text.pollinations.ai/openai",
        default_quick_model="openai",
        default_report_model="openai-large",
        is_free=True,
        requires_api_key=False,
        env_key_name="",
        signup_url="https://pollinations.ai",
        docs_url="https://github.com/pollinations/pollinations/blob/master/APIDOCS.md",
        description=(
            "Zero-config community OpenAI-compatible endpoint requiring NO API key. "
            "Ideal for instant keyless answers and automatic fallback."
        ),
        recommended_models=[
            "openai",
            "openai-large",
            "mistral",
            "llama",
            "deepseek",
            "qwen-coder",
        ],
    ),
    "gemini": LLMProviderPreset(
        id="gemini",
        name="Google Gemini AI Studio (Free Tier)",
        protocol="openai",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        default_quick_model="gemini-2.0-flash",
        default_report_model="gemini-2.5-flash",
        is_free=True,
        requires_api_key=True,
        env_key_name="GEMINI_API_KEY",
        signup_url="https://aistudio.google.com/apikey",
        docs_url="https://ai.google.dev/gemini-api/docs/openai",
        description=(
            "Google AI Studio OpenAI-compatible endpoint with generous free tier "
            "(up to 1,500 req/day on Flash models, 1M token context)."
        ),
        recommended_models=[
            "gemini-2.0-flash",
            "gemini-2.5-flash",
            "gemini-2.0-flash-lite",
            "gemini-2.5-pro",
        ],
    ),
    "groq": LLMProviderPreset(
        id="groq",
        name="Groq Cloud LPU (Free Developer Tier)",
        protocol="openai",
        base_url="https://api.groq.com/openai/v1",
        default_quick_model="llama-3.3-70b-versatile",
        default_report_model="llama-3.3-70b-versatile",
        is_free=True,
        requires_api_key=True,
        env_key_name="GROQ_API_KEY",
        signup_url="https://console.groq.com/keys",
        docs_url="https://console.groq.com/docs/openai",
        description=(
            "Ultra-fast LPU inference (300-800 tok/s) with a free developer tier "
            "for Llama 3.3 70B, Llama 3.1 8B, DeepSeek R1 Distill, and Qwen."
        ),
        recommended_models=[
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "deepseek-r1-distill-llama-70b",
            "qwen-qwq-32b",
        ],
    ),
    "cerebras": LLMProviderPreset(
        id="cerebras",
        name="Cerebras Wafer-Scale Inference (Free Tier)",
        protocol="openai",
        base_url="https://api.cerebras.ai/v1",
        default_quick_model="llama-3.3-70b",
        default_report_model="llama-3.3-70b",
        is_free=True,
        requires_api_key=True,
        env_key_name="CEREBRAS_API_KEY",
        signup_url="https://cloud.cerebras.ai",
        docs_url="https://inference-docs.cerebras.ai/openai",
        description=(
            "Wafer-scale inference delivering >2,000 tokens/sec with a free developer tier "
            "(up to 1M tokens/day)."
        ),
        recommended_models=[
            "llama-3.3-70b",
            "llama3.1-8b",
            "qwen-3-32b",
        ],
    ),
    "mistral": LLMProviderPreset(
        id="mistral",
        name="Mistral AI La Plateforme (Free Experiment Tier)",
        protocol="openai",
        base_url="https://api.mistral.ai/v1",
        default_quick_model="mistral-small-latest",
        default_report_model="mistral-small-latest",
        is_free=True,
        requires_api_key=True,
        env_key_name="MISTRAL_API_KEY",
        signup_url="https://console.mistral.ai/api-keys",
        docs_url="https://docs.mistral.ai/api/",
        description=(
            "Official Mistral AI API offering a free Experiment plan for Mistral Small 3.1, "
            "Open Mistral Nemo, and Codestral."
        ),
        recommended_models=[
            "mistral-small-latest",
            "open-mistral-nemo",
            "codestral-latest",
            "mistral-large-latest",
        ],
    ),
    "github_models": LLMProviderPreset(
        id="github_models",
        name="GitHub Models (Free Azure AI Inference)",
        protocol="openai",
        base_url="https://models.inference.ai.azure.com",
        default_quick_model="gpt-4o-mini",
        default_report_model="gpt-4o",
        is_free=True,
        requires_api_key=True,
        env_key_name="GITHUB_TOKEN",
        signup_url="https://github.com/marketplace/models",
        docs_url="https://docs.github.com/en/github-models",
        description=(
            "Free developer access to GPT-4o-mini, GPT-4o, Llama 3.3 70B, and DeepSeek R1 "
            "authenticated via a standard GitHub Personal Access Token."
        ),
        recommended_models=[
            "gpt-4o-mini",
            "gpt-4o",
            "Llama-3.3-70B-Instruct",
            "DeepSeek-R1",
        ],
    ),
    "huggingface": LLMProviderPreset(
        id="huggingface",
        name="HuggingFace Inference Router (Free Tier)",
        protocol="openai",
        base_url="https://router.huggingface.co/v1",
        default_quick_model="Qwen/Qwen2.5-72B-Instruct",
        default_report_model="Qwen/Qwen2.5-72B-Instruct",
        is_free=True,
        requires_api_key=True,
        env_key_name="HF_TOKEN",
        signup_url="https://huggingface.co/settings/tokens",
        docs_url="https://huggingface.co/docs/api-inference/index",
        description=(
            "HuggingFace serverless OpenAI-compatible router for open-weight models "
            "(Qwen 2.5 72B, Llama 3.3 70B, DeepSeek R1 Distill)."
        ),
        recommended_models=[
            "Qwen/Qwen2.5-72B-Instruct",
            "meta-llama/Llama-3.3-70B-Instruct",
            "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B",
        ],
    ),
    "ollama": LLMProviderPreset(
        id="ollama",
        name="Ollama / vLLM / LM Studio (Local Self-Hosted Free)",
        protocol="openai",
        base_url="http://127.0.0.1:11434/v1",
        default_quick_model="qwen2.5:7b",
        default_report_model="qwen2.5:14b",
        is_free=True,
        requires_api_key=False,
        env_key_name="",
        signup_url="https://ollama.com",
        docs_url="https://github.com/ollama/ollama/blob/main/docs/openai.md",
        description=(
            "100% private local inference via Ollama, vLLM, LM Studio, or llama.cpp "
            "with zero external API cost."
        ),
        recommended_models=[
            "qwen2.5:7b",
            "qwen2.5:14b",
            "llama3.2:3b",
            "llama3.1:8b",
            "deepseek-r1:8b",
            "gemma3:12b",
        ],
    ),
    "custom": LLMProviderPreset(
        id="custom",
        name="Custom / External System LLM (BYO Endpoint / LiteLLM / Internal Gateway)",
        protocol="openai",
        base_url="",
        default_quick_model="gpt-4o-mini",
        default_report_model="gpt-4o",
        is_free=False,
        requires_api_key=False,
        env_key_name="CUSTOM_LLM_API_KEY",
        signup_url="",
        docs_url="",
        description=(
            "Connect MetaSearch to an existing LLM already deployed in your system "
            "(LiteLLM Proxy, vLLM Cluster, Kong AI Gateway, Azure OpenAI, AWS Bedrock Gateway, "
            "or any OpenAI/Anthropic-compatible endpoint) with custom URL, API key, and headers."
        ),
        recommended_models=[
            "gpt-4o-mini",
            "gpt-4o",
            "claude-3-5-sonnet-latest",
            "llama-3.3-70b-instruct",
        ],
    ),
    "openai": LLMProviderPreset(
        id="openai",
        name="OpenAI Official API",
        protocol="openai",
        base_url="https://api.openai.com/v1",
        default_quick_model="gpt-4o-mini",
        default_report_model="gpt-4o",
        is_free=False,
        requires_api_key=True,
        env_key_name="OPENAI_API_KEY",
        signup_url="https://platform.openai.com/api-keys",
        docs_url="https://platform.openai.com/docs",
        description="Official OpenAI Chat Completions API.",
        recommended_models=["gpt-4o-mini", "gpt-4o", "o3-mini"],
    ),
    "anthropic": LLMProviderPreset(
        id="anthropic",
        name="Anthropic Claude Official API",
        protocol="anthropic",
        base_url="https://api.anthropic.com",
        default_quick_model="claude-3-5-haiku-latest",
        default_report_model="claude-3-5-sonnet-latest",
        is_free=False,
        requires_api_key=True,
        env_key_name="ANTHROPIC_API_KEY",
        signup_url="https://console.anthropic.com",
        docs_url="https://docs.anthropic.com",
        description="Official Anthropic Messages API for Claude 3.5 Sonnet and Haiku.",
        recommended_models=["claude-3-5-haiku-latest", "claude-3-5-sonnet-latest"],
    ),
    "none": LLMProviderPreset(
        id="none",
        name="Disabled (Search & Context-Only Mode for External LLM)",
        protocol="none",
        base_url="",
        default_quick_model="none",
        default_report_model="none",
        is_free=True,
        requires_api_key=False,
        env_key_name="",
        signup_url="",
        docs_url="",
        description=(
            "Disable internal LLM synthesis. Ideal when an external system calls "
            "/v1/search or /v1/context and runs synthesis on its own internal LLM."
        ),
        recommended_models=[],
    ),
}


class LLMOverride(BaseModel):
    """Per-request or runtime BYO-LLM configuration override.

    Allows an external system or user to route a specific request (or the whole
    server via ``PATCH /v1/llm/config``) to OpenRouter Free, Gemini, Groq, Ollama,
    or their own internal LLM endpoint.
    """

    provider: ProviderId | None = None
    protocol: Literal["openai", "anthropic"] | None = None
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=150)
    quick_model: str | None = Field(default=None, max_length=150)
    report_model: str | None = Field(default=None, max_length=150)
    fallback_provider: str | None = Field(default=None, max_length=50)
    extra_headers: dict[str, str] | None = None


def validate_llm_base_url(url: str) -> str:
    """Validate an LLM base URL, blocking cloud metadata endpoints (SSRF guard)."""
    cleaned = url.strip().rstrip("/")
    if not cleaned:
        return ""
    parsed = urlsplit(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise MetaSearchError(
            ErrorCode.INVALID_REQUEST,
            "LLM base_url must be a valid http:// or https:// URL",
        )
    host = parsed.hostname.lower()
    if host in _BLOCKED_METADATA_HOSTS:
        raise MetaSearchError(
            ErrorCode.INVALID_REQUEST,
            f"LLM base_url host '{host}' is blocked for security reasons",
        )
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise MetaSearchError(
                ErrorCode.INVALID_REQUEST,
                f"LLM base_url IP '{host}' is in a forbidden address range",
            )
    except ValueError:
        pass
    return cleaned


def mask_api_key(key: str) -> str:
    """Return a safe masked representation of an API key for UI/API inspection."""
    k = (key or "").strip()
    if not k:
        return ""
    if len(k) <= 8:
        return "***"
    return f"{k[:4]}...{k[-4:]}"


def parse_extra_headers(raw: str | dict[str, str] | None) -> dict[str, str]:
    """Parse extra headers from a JSON string or dict, stripping unsafe hop headers."""
    if not raw:
        return {}
    data: dict[str, Any] = {}
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        raw_str = raw.strip()
        if not raw_str:
            return {}
        try:
            parsed = json.loads(raw_str)
            if isinstance(parsed, dict):
                data = parsed
        except json.JSONDecodeError:
            return {}
    forbidden = {"host", "content-length", "transfer-encoding", "connection"}
    return {
        str(k).strip(): str(v).strip()
        for k, v in data.items()
        if str(k).strip() and str(k).strip().lower() not in forbidden
    }


@dataclass(slots=True)
class ResolvedLLMTarget:
    """Concrete endpoint parameters resolved for a single LLM call."""

    provider: str
    protocol: ProtocolType
    base_url: str
    api_key: str
    quick_model: str
    report_model: str
    extra_headers: dict[str, str]
    fallback_models: list[str]
    fallback_provider: str


class RuntimeLLMConfigStore:
    """In-process hot-reloadable LLM configuration store (similar to EngineConfigStore)."""

    def __init__(self) -> None:
        self._override: LLMOverride | None = None

    def get_override(self) -> LLMOverride | None:
        return self._override

    def update(self, patch: LLMOverride) -> LLMOverride:
        if patch.base_url:
            patch.base_url = validate_llm_base_url(patch.base_url)
        if self._override is None:
            self._override = patch
        else:
            merged = self._override.model_dump(exclude_none=True)
            merged.update(patch.model_dump(exclude_none=True))
            self._override = LLMOverride(**merged)
        return self._override

    def reset(self) -> None:
        self._override = None


_runtime_store = RuntimeLLMConfigStore()


def get_runtime_llm_store() -> RuntimeLLMConfigStore:
    return _runtime_store


def _provider_env_api_key(settings: Settings, provider: str) -> str:
    """Lookup the configured API key for ``provider`` from ``Settings``."""
    mapping = {
        "openai": settings.openai_api_key,
        "anthropic": settings.anthropic_api_key,
        "openrouter": settings.openrouter_api_key,
        "gemini": settings.gemini_api_key,
        "groq": settings.groq_api_key,
        "cerebras": settings.cerebras_api_key,
        "mistral": settings.mistral_api_key,
        "github_models": settings.github_token,
        "huggingface": settings.hf_token,
        "pollinations": "pollinations-free",
        "ollama": "ollama-local",
        "custom": settings.custom_llm_api_key or settings.openai_api_key,
        "none": "",
    }
    return mapping.get(provider, "")


def _provider_env_base_url(settings: Settings, provider: str) -> str:
    """Lookup the configured base_url for ``provider`` from ``Settings``."""
    preset = LLM_PROVIDER_PRESETS.get(provider)
    default_url = preset.base_url if preset else ""
    mapping = {
        "openai": settings.openai_base_url,
        "anthropic": settings.anthropic_base_url,
        "openrouter": settings.openrouter_base_url,
        "gemini": settings.gemini_base_url,
        "groq": settings.groq_base_url,
        "cerebras": settings.cerebras_base_url,
        "mistral": settings.mistral_base_url,
        "github_models": settings.github_models_base_url,
        "huggingface": settings.hf_base_url,
        "pollinations": settings.pollinations_base_url,
        "ollama": settings.ollama_base_url,
        "custom": settings.custom_llm_base_url or settings.openai_base_url,
        "none": "",
    }
    return (mapping.get(provider) or default_url).rstrip("/")


def resolve_llm_target(
    settings: Settings,
    *,
    runtime_override: LLMOverride | None = None,
    request_override: LLMOverride | None = None,
) -> ResolvedLLMTarget:
    """Resolve effective LLM provider, base_url, api_key, models, and headers.

    Precedence: ``request_override`` > ``runtime_override`` > ``settings``.
    """
    eff_override = LLMOverride()
    for ov in (runtime_override, request_override):
        if ov is not None:
            merged = eff_override.model_dump(exclude_none=True)
            merged.update(ov.model_dump(exclude_none=True))
            eff_override = LLMOverride(**merged)

    provider = eff_override.provider or settings.llm_provider
    preset = LLM_PROVIDER_PRESETS.get(provider, LLM_PROVIDER_PRESETS["custom"])

    protocol: ProtocolType = eff_override.protocol or (
        settings.custom_llm_protocol if provider == "custom" else preset.protocol
    )
    if provider == "none":
        protocol = "none"

    base_url = (eff_override.base_url or _provider_env_base_url(settings, provider)).rstrip("/")
    if eff_override.base_url:
        base_url = validate_llm_base_url(base_url)

    api_key = eff_override.api_key
    if api_key is None:
        api_key = _provider_env_api_key(settings, provider)
    if not api_key and provider in ("pollinations", "ollama"):
        api_key = f"{provider}-free"

    # Smart model resolution: if user switched provider away from openai without
    # explicitly changing llm_quick_model from the stock "gpt-4o-mini", use the
    # selected provider's native default model (e.g., "openrouter/free").
    stock_quick = "gpt-4o-mini"
    stock_report = "gpt-4o"

    if eff_override.model:
        quick_model = eff_override.model
        report_model = eff_override.model
    else:
        if eff_override.quick_model:
            quick_model = eff_override.quick_model
        elif provider != "openai" and settings.llm_quick_model == stock_quick:
            quick_model = preset.default_quick_model
        else:
            quick_model = settings.llm_quick_model or preset.default_quick_model

        if eff_override.report_model:
            report_model = eff_override.report_model
        elif provider != "openai" and settings.llm_report_model == stock_report:
            report_model = preset.default_report_model
        else:
            report_model = settings.llm_report_model or preset.default_report_model

    headers: dict[str, str] = {}
    if provider == "openrouter":
        if settings.openrouter_site_url:
            headers["HTTP-Referer"] = settings.openrouter_site_url
        if settings.openrouter_app_name:
            headers["X-Title"] = settings.openrouter_app_name
    if settings.custom_llm_headers:
        headers.update(parse_extra_headers(settings.custom_llm_headers))
    if eff_override.extra_headers:
        headers.update(parse_extra_headers(eff_override.extra_headers))

    fallback_models: list[str] = []
    if provider == "openrouter" and settings.openrouter_fallback_models:
        fallback_models = [
            m.strip() for m in settings.openrouter_fallback_models.split(",") if m.strip()
        ]

    fallback_provider = (
        eff_override.fallback_provider
        if eff_override.fallback_provider is not None
        else settings.llm_fallback_provider
    ).strip()

    return ResolvedLLMTarget(
        provider=provider,
        protocol=protocol,
        base_url=base_url,
        api_key=api_key,
        quick_model=quick_model,
        report_model=report_model,
        extra_headers=headers,
        fallback_models=fallback_models,
        fallback_provider=fallback_provider,
    )
