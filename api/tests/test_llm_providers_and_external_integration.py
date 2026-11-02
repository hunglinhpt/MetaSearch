"""Tests for OpenRouter Free (openrouter/free), multi-provider free LLM presets,
runtime hot-config (/v1/llm/*), and External System LLM integration
(/v1/context, /v1/citations/validate, and per-request BYO-LLM overrides).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

import app.api.deps as deps
from app.config import Settings
from app.core.errors import ErrorCode, MetaSearchError
from app.extraction.scraper import PageContent
from app.llm.gateway import LLMGateway, Usage
from app.llm.providers import (
    LLM_PROVIDER_PRESETS,
    LLMOverride,
    get_runtime_llm_store,
    mask_api_key,
    resolve_llm_target,
    validate_llm_base_url,
)
from app.main import app
from app.orchestration.engine_store import InMemoryEngineStore
from app.retrieval.models import EngineConfig, RawResult
from app.retrieval.reranker import HeuristicReranker


def _sse_response(events: list[str]) -> httpx.Response:
    body = "".join(f"data: {e}\n\n" for e in events) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


class TestProviderPresetsAndResolution:
    def test_openrouter_free_preset_is_configured(self) -> None:
        preset = LLM_PROVIDER_PRESETS["openrouter"]
        assert preset.default_quick_model == "openrouter/free"
        assert preset.default_report_model == "openrouter/free"
        assert preset.base_url == "https://openrouter.ai/api/v1"
        assert preset.signup_url == "https://openrouter.ai/openrouter/free"
        assert preset.is_free is True
        assert "openrouter/free" in preset.recommended_models

    def test_reputable_free_and_custom_presets_present(self) -> None:
        expected = {
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
            "openai",
            "anthropic",
            "none",
        }
        assert expected.issubset(set(LLM_PROVIDER_PRESETS.keys()))
        assert LLM_PROVIDER_PRESETS["pollinations"].requires_api_key is False
        assert LLM_PROVIDER_PRESETS["ollama"].requires_api_key is False

    def test_smart_model_resolution_for_openrouter(self) -> None:
        settings = Settings(
            _env_file=None,
            ENVIRONMENT="test",
            LLM_PROVIDER="openrouter",
            OPENROUTER_API_KEY="sk-or-v1-secret9876",
        )
        target = resolve_llm_target(settings)
        assert target.provider == "openrouter"
        assert target.quick_model == "openrouter/free"
        assert target.report_model == "openrouter/free"
        assert target.base_url == "https://openrouter.ai/api/v1"
        assert target.api_key == "sk-or-v1-secret9876"
        assert "HTTP-Referer" in target.extra_headers
        assert "X-Title" in target.extra_headers

    def test_free_models_have_zero_cost(self) -> None:
        usage = Usage(input_tokens=500_000, output_tokens=200_000)
        assert usage.cost_usd("openrouter/free") == 0.0
        assert usage.cost_usd("meta-llama/llama-3.3-70b-instruct:free") == 0.0
        assert usage.cost_usd("deepseek/deepseek-r1:free") == 0.0

    def test_ssrf_guard_on_llm_base_url(self) -> None:
        assert (
            validate_llm_base_url("https://openrouter.ai/api/v1/") == "https://openrouter.ai/api/v1"
        )
        # Private loopback allowed only when allow_private=True (e.g. server env / admin config)
        assert (
            validate_llm_base_url("http://127.0.0.1:11434/v1", allow_private=True)
            == "http://127.0.0.1:11434/v1"
        )
        with pytest.raises(MetaSearchError) as exc_priv:
            validate_llm_base_url("http://127.0.0.1:11434/v1", allow_private=False)
        assert exc_priv.value.code == ErrorCode.INVALID_REQUEST

        with pytest.raises(MetaSearchError) as exc_info:
            validate_llm_base_url("http://169.254.169.254/latest/meta-data")
        assert exc_info.value.code == ErrorCode.INVALID_REQUEST

        with pytest.raises(MetaSearchError) as exc_info2:
            validate_llm_base_url("http://metadata.google.internal/computeMetadata/v1")
        assert exc_info2.value.code == ErrorCode.INVALID_REQUEST

    def test_mask_api_key(self) -> None:
        assert mask_api_key("") == ""
        assert mask_api_key("short") == "***"
        assert mask_api_key("sk-or-v1-1234567890abcd") == "sk-o...abcd"


class TestOpenRouterGatewayAndFailover:
    async def test_openrouter_free_headers_and_stream(self) -> None:
        captured_req: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_req["url"] = str(request.url)
            captured_req["headers"] = dict(request.headers)
            captured_req["body"] = json.loads(request.content)
            return _sse_response(
                [
                    json.dumps({"choices": [{"delta": {"content": "OpenRouter Free OK [1]"}}]}),
                    json.dumps(
                        {
                            "choices": [{"delta": {}}],
                            "usage": {"prompt_tokens": 15, "completion_tokens": 5},
                        }
                    ),
                ]
            )

        settings = Settings(
            _env_file=None,
            ENVIRONMENT="test",
            LLM_PROVIDER="openrouter",
            OPENROUTER_API_KEY="sk-or-test-key",
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gw = LLMGateway(settings, client=client, use_runtime_store=False)

        parts: list[str] = []
        async for delta in gw.stream([{"role": "user", "content": "hello"}]):
            if delta.text:
                parts.append(delta.text)

        assert "".join(parts) == "OpenRouter Free OK [1]"
        assert captured_req["url"] == "https://openrouter.ai/api/v1/chat/completions"
        body = captured_req["body"]
        assert isinstance(body, dict)
        assert body["model"] == "openrouter/free"
        headers = captured_req["headers"]
        assert isinstance(headers, dict)
        assert headers["authorization"] == "Bearer sk-or-test-key"
        assert "http-referer" in headers
        assert "x-title" in headers

    async def test_openrouter_free_model_failover_on_429(self) -> None:
        models_tried: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            model = body["model"]
            models_tried.append(model)
            if model == "openrouter/free":
                return httpx.Response(429, text="free router busy")
            return _sse_response(
                [json.dumps({"choices": [{"delta": {"content": f"Fallback from {model}"}}]})]
            )

        settings = Settings(
            _env_file=None,
            ENVIRONMENT="test",
            LLM_PROVIDER="openrouter",
            OPENROUTER_API_KEY="sk-or-test-key",
            OPENROUTER_FALLBACK_MODELS="openrouter/free,meta-llama/llama-3.3-70b-instruct:free",
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gw = LLMGateway(settings, client=client, use_runtime_store=False)

        parts = [d.text async for d in gw.stream([{"role": "user", "content": "hi"}]) if d.text]
        assert "".join(parts) == "Fallback from meta-llama/llama-3.3-70b-instruct:free"
        assert models_tried == ["openrouter/free", "meta-llama/llama-3.3-70b-instruct:free"]

    async def test_cross_provider_fallback_to_pollinations_keyless(self) -> None:
        urls_called: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            urls_called.append(str(request.url))
            return _sse_response(
                [json.dumps({"choices": [{"delta": {"content": "Pollinations keyless reply"}}]})]
            )

        # Primary provider is openrouter WITHOUT an API key, fallback is pollinations
        settings = Settings(
            _env_file=None,
            ENVIRONMENT="test",
            LLM_PROVIDER="openrouter",
            OPENROUTER_API_KEY="",
            LLM_FALLBACK_PROVIDER="pollinations",
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gw = LLMGateway(settings, client=client, use_runtime_store=False)

        parts = [d.text async for d in gw.stream([{"role": "user", "content": "hi"}]) if d.text]
        assert "".join(parts) == "Pollinations keyless reply"
        assert urls_called == ["https://text.pollinations.ai/openai/chat/completions"]

    async def test_non_streaming_json_proxy_compatibility(self) -> None:
        """Enterprise internal LLM gateways returning application/json instead of SSE work."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "Internal Enterprise LLM Reply [1]"}}],
                    "usage": {"prompt_tokens": 25, "completion_tokens": 8},
                },
                headers={"content-type": "application/json"},
            )

        settings = Settings(
            _env_file=None,
            ENVIRONMENT="test",
            LLM_PROVIDER="custom",
            CUSTOM_LLM_BASE_URL="https://llm-gateway.internal.corp/v1",
            CUSTOM_LLM_API_KEY="corp-token",
            CUSTOM_LLM_HEADERS='{"X-Tenant": "quant-desk"}',
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gw = LLMGateway(settings, client=client, use_runtime_store=False)

        text_parts: list[str] = []
        total_in = 0
        async for delta in gw.stream([{"role": "user", "content": "hi"}]):
            if delta.text:
                text_parts.append(delta.text)
            if delta.usage:
                total_in += delta.usage.input_tokens
        assert "".join(text_parts) == "Internal Enterprise LLM Reply [1]"
        assert total_in == 25


class TestLLMEndpointsAndExternalIntegration:
    @pytest.fixture(autouse=True)
    def _setup_fake_stack(self, monkeypatch):
        get_runtime_llm_store().reset()
        raw_results = [
            RawResult(
                title="Báo cáo tài chính VCB Q4",
                url="https://example.com/vcb-bctc",
                snippet="VCB ghi nhận NIM 3.2%, CASA 34%, nợ xấu NPL 0.98% và LLR 210%.",
                adapter="viet_stock",
                rank=1,
                published_at=datetime.now(UTC),
            ),
            RawResult(
                title="Phân tích kỹ thuật FPT",
                url="https://example.com/fpt-tech",
                snippet="Cổ phiếu FPT vượt kháng cự Fibonacci 61.8% với RSI 64.",
                adapter="viet_stock",
                rank=2,
                published_at=datetime.now(UTC),
            ),
        ]

        class _Adapter:
            id = "viet_stock"

            async def search(self, query, **kwargs):
                return raw_results

        class _Scraper:
            async def scrape_many(self, urls, **kwargs):
                return [
                    PageContent(
                        url=u,
                        final_url=u,
                        title="VCB & FPT Financial Analysis",
                        markdown=(
                            "Vietcombank (VCB) duy trì tỷ lệ CASA 34% và LLR 210% "
                            "dẫn đầu ngành ngân hàng. "
                            "FPT tăng trưởng 20% doanh thu mảng AI và bán dẫn."
                        ),
                        extractor_used="trafilatura",
                        fetched_at=datetime.now(UTC),
                    )
                    for u in urls
                ]

        class _FakeGateway:
            def __init__(self, override: LLMOverride | None = None):
                self._override = override

            def with_override(self, override: LLMOverride | None):
                return _FakeGateway(override)

            @property
            def default_quick_model(self) -> str:
                if self._override and self._override.model:
                    return self._override.model
                if self._override and self._override.provider == "openrouter":
                    return "openrouter/free"
                return "openrouter/free"

            async def stream(self, messages, *, model=None, max_tokens=None):
                from app.llm.gateway import StreamDelta

                yield StreamDelta(
                    text=f"VCB có CASA 34% [1] và FPT tăng trưởng 20% [2] (via {model})."
                )
                yield StreamDelta(usage=Usage(input_tokens=40, output_tokens=15))

        store = InMemoryEngineStore(
            {
                "viet_stock": EngineConfig(
                    id="viet_stock", name="VietStock", category="general", enabled=True
                )
            }
        )
        monkeypatch.setattr(deps, "get_engine_store", lambda: store)
        monkeypatch.setattr(deps, "build_adapters", lambda s: {"viet_stock": _Adapter()})
        monkeypatch.setattr(deps, "get_scraper", lambda: _Scraper())
        monkeypatch.setattr(deps, "get_gateway", lambda: _FakeGateway())
        monkeypatch.setattr(deps, "get_reranker", lambda: HeuristicReranker())
        deps.reset_pipelines()
        yield
        get_runtime_llm_store().reset()

    def test_get_and_patch_llm_config(self) -> None:
        client = TestClient(app)
        resp = client.get("/v1/llm/providers")
        assert resp.status_code == 200
        data = resp.json()
        provider_ids = {p["id"] for p in data["providers"]}
        assert "openrouter" in provider_ids
        assert "pollinations" in provider_ids
        assert "custom" in provider_ids

        # Hot-switch to OpenRouter Free
        patch_resp = client.patch(
            "/v1/llm/config",
            json={
                "provider": "openrouter",
                "api_key": "sk-or-v1-livekey9999",
                "quick_model": "openrouter/free",
                "fallback_provider": "pollinations",
            },
        )
        assert patch_resp.status_code == 200
        active = patch_resp.json()
        assert active["provider"] == "openrouter"
        assert active["quick_model"] == "openrouter/free"
        assert active["has_api_key"] is True
        assert active["masked_api_key"] == "sk-o...9999"
        assert active["has_runtime_override"] is True

        # Test connectivity endpoint
        test_resp = client.post("/v1/llm/test", json={})
        assert test_resp.status_code == 200
        assert test_resp.json()["ok"] is True

        # Reset back to env defaults
        reset_resp = client.post("/v1/llm/config/reset")
        assert reset_resp.status_code == 200
        assert reset_resp.json()["has_runtime_override"] is False

    def test_external_system_context_and_citation_validation(self) -> None:
        client = TestClient(app)
        # Step 1: External system calls POST /v1/context to get RAG pack for its own LLM
        ctx_resp = client.post(
            "/v1/context",
            json={"query": "Phân tích CASA của VCB và tăng trưởng FPT", "k": 5, "budget": 4000},
        )
        assert ctx_resp.status_code == 200
        ctx = ctx_resp.json()
        assert "context_md" in ctx
        assert "[1]" in ctx["context_md"]
        assert len(ctx["messages"]) == 2
        assert ctx["messages"][0]["role"] == "system"
        assert len(ctx["chunks"]) >= 1
        assert len(ctx["sources"]) == 2

        # Step 2: External system runs its own LLM and validates citations via MetaSearch
        val_resp = client.post(
            "/v1/citations/validate",
            json={
                "answer_md": "VCB đạt CASA 34% [1] trong khi nhận định giả mạo [99] bị loại bỏ.",
                "chunks": ctx["chunks"],
            },
        )
        assert val_resp.status_code == 200
        validated = val_resp.json()
        assert "[1]" in validated["validated_answer_md"]
        assert "[99]" not in validated["validated_answer_md"]
        assert validated["invalid_markers"] == [99]
        assert validated["citations"][0]["n"] == 1

    def test_answer_with_per_request_byo_llm_override(self) -> None:
        client = TestClient(app)
        resp = client.post(
            "/v1/answer",
            json={
                "query": "Phân tích VCB",
                "stream": False,
                "llm": {
                    "provider": "openrouter",
                    "model": "openrouter/free",
                },
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["model"] == "openrouter/free"
        assert "via openrouter/free" in body["answer_md"]
        assert len(body["citations"]) >= 1
