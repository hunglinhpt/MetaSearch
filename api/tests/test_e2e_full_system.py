"""Complete End-to-End (E2E) Verification Suite for MetaSearch v1.2+.

This E2E suite is a mandatory pre-commit & pre-release gate. No commit or release
is permitted unless every test in this file passes with 0 errors.

Covers end-to-end:
1. Free LLM Directories (nejib1/Free-LLM, open-free-llm-api/awesome-freellm-apis,
   OpenRouter Free Router) and all 20 LLM provider presets.
2. Universal Multi-Domain Deep Intelligence (8 domains with 'financial' as Flagship Pilot).
3. Multi-Free-Endpoint LLM Council + Anonymous Cross-Review + Dream-RSI Replay World
   self-improvement across multiple generations, including partial endpoint failure resilience.
4. End-to-End /v1/answer in both JSON and SSE streaming modes with council_mode=True.
5. End-to-End External System LLM Integration (/v1/context -> /v1/citations/validate).
6. End-to-End Search (/v1/search), Engine Registry (/v1/engines), Health (/healthz),
   and Daily Briefings (/v1/digests/topics).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import app.api.deps as deps
from app.extraction.scraper import PageContent
from app.llm.council import get_dream_rsi_store
from app.llm.gateway import StreamDelta, Usage
from app.llm.providers import (
    FREE_LLM_DIRECTORY_SOURCES,
    LLM_PROVIDER_PRESETS,
    LLMOverride,
    get_runtime_llm_store,
)
from app.main import app
from app.orchestration.engine_store import InMemoryEngineStore
from app.retrieval.models import EngineConfig, RawResult
from app.retrieval.reranker import HeuristicReranker


class _CouncilAwareFakeGateway:
    """Simulates multiple free LLM endpoints for Council generation, Peer Cross-Review,
    and Chairman synthesis, including optional simulated failures on specific providers.
    """

    def __init__(
        self,
        override: LLMOverride | None = None,
        *,
        fail_providers: set[str] | None = None,
    ) -> None:
        self._override = override
        self._fail_providers = fail_providers or set()

    def with_override(self, override: LLMOverride | None) -> _CouncilAwareFakeGateway:
        return _CouncilAwareFakeGateway(override, fail_providers=self._fail_providers)

    @property
    def default_quick_model(self) -> str:
        if self._override and self._override.model:
            return self._override.model
        if self._override and self._override.provider:
            preset = LLM_PROVIDER_PRESETS.get(self._override.provider)
            if preset and preset.default_quick_model:
                return preset.default_quick_model
        return "openrouter/free"

    async def stream(self, messages, *, model=None, max_tokens=None, temperature=0.2):
        provider = (self._override.provider if self._override else None) or "openrouter"
        chosen_model = model or self.default_quick_model

        if provider in self._fail_providers:
            raise RuntimeError(f"Simulated 429 rate limit on {provider}:{chosen_model}")

        user_text = messages[-1]["content"] if messages else ""
        sys_text = messages[0]["content"] if messages else ""

        # 1. Peer Cross-Review JSON response
        if "Ban Giám Khảo" in sys_text or ("Candidate A" in user_text and "scores" in user_text):
            review_payload = {
                "scores": {"Candidate A": 92, "Candidate B": 84, "Candidate C": 79},
                "winner": "Candidate A",
                "critique_summary": (
                    f"Reviewer {provider}:{chosen_model} xác nhận Candidate A trích dẫn "
                    "đầy đủ số liệu NIM, CASA, NPL và LLR chính xác theo nguồn [1][2]."
                ),
            }
            yield StreamDelta(text=json.dumps(review_payload, ensure_ascii=False))
            yield StreamDelta(usage=Usage(input_tokens=120, output_tokens=45))
            return

        # 2. Chairman synthesis response
        if "Chủ tịch Hội đồng Phán quyết" in sys_text or "Phán quyết Tổng hợp" in user_text:
            verdict_md = (
                "## Phán Quyết Hội Đồng Thẩm Định Chuyên Sâu\n"
                "- **Chất lượng tài sản & NIM (VCB)**: NIM đạt 3.25%, tỷ lệ CASA 34.5%, "
                "nợ xấu NPL chỉ 0.96% và bộ đệm bao phủ nợ xấu LLR đạt 215% [1].\n"
                "- **Định lượng & Kỹ thuật (FPT)**: Doanh thu mảng AI & Bán dẫn tăng 24.8% YoY, "
                "RSI(14) ở mức 63.5 và giữ vững hỗ trợ Fibonacci 61.8% [2].\n"
            )
            yield StreamDelta(text=verdict_md)
            yield StreamDelta(usage=Usage(input_tokens=200, output_tokens=80))
            return

        # 3. Panelist candidate generation
        if provider == "openrouter":
            draft = (
                "Phân tích VCB: NIM 3.25%, CASA 34.5%, NPL 0.96%, LLR 215% [1]. "
                "FPT tăng trưởng 24.8% YoY với RSI 63.5 trên mốc Fibonacci 61.8% [2]."
            )
        elif provider == "pollinations":
            draft = (
                "Theo báo cáo tài chính [1], VCB duy trì CASA 34.5% và LLR 215%. "
                "Cổ phiếu FPT có xung lực kỹ thuật tích cực tại Fibonacci 61.8% [2]."
            )
        else:
            draft = (
                f"Đánh giá từ {provider}/{chosen_model}: VCB có NPL 0.96% [1] và "
                "FPT tăng trưởng 24.8% nhờ mảng AI [2]."
            )
        yield StreamDelta(text=draft)
        yield StreamDelta(usage=Usage(input_tokens=90, output_tokens=40))


@pytest.fixture()
def e2e_client(monkeypatch):
    """Configures a deterministic full-stack E2E environment for FastAPI TestClient."""
    get_runtime_llm_store().reset()
    get_dream_rsi_store().reset()

    raw_results = [
        RawResult(
            title="Pháp y BCTC Vietcombank (VCB) Quý 4 — CAMELS & Chất lượng tài sản",
            url="https://finance.example.com/vcb-camels-q4",
            snippet="VCB ghi nhận NIM 3.25%, CASA 34.5%, nợ xấu NPL 0.96% và LLR 215%.",
            adapter="viet_stock",
            rank=1,
            published_at=datetime.now(UTC),
        ),
        RawResult(
            title="Phân tích Định lượng & Kỹ thuật FPT — Fibonacci & Ichimoku",
            url="https://finance.example.com/fpt-quant-tech",
            snippet="FPT tăng trưởng 24.8% YoY, RSI(14) đạt 63.5, giữ vững Fibonacci 61.8%.",
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
            pages = []
            for idx, u in enumerate(urls):
                if idx == 0:
                    md = (
                        "Báo cáo kiểm toán VCB: Tỷ lệ NIM đạt 3.25%, CASA 34.5%, "
                        "tỷ lệ nợ xấu NPL 0.96%, tỷ lệ bao phủ nợ xấu LLR 215%, CAR 11.8%."
                    )
                else:
                    md = (
                        "Phân tích FPT: Doanh thu mảng AI & Cloud tăng trưởng 24.8% YoY. "
                        "Chỉ báo RSI(14) đạt 63.5, giá nằm trên vùng Fibonacci Golden Pocket 61.8%."
                    )
                pages.append(
                    PageContent(
                        url=u,
                        final_url=u,
                        title=f"Source Document {idx + 1}",
                        markdown=md,
                        extractor_used="trafilatura",
                        fetched_at=datetime.now(UTC),
                    )
                )
            return pages

    store = InMemoryEngineStore(
        {
            "viet_stock": EngineConfig(
                id="viet_stock", name="VietStock Forensics", category="general", enabled=True
            )
        }
    )
    gateway = _CouncilAwareFakeGateway()
    monkeypatch.setattr(deps, "get_engine_store", lambda: store)
    monkeypatch.setattr(deps, "build_adapters", lambda s: {"viet_stock": _Adapter()})
    monkeypatch.setattr(deps, "get_scraper", lambda: _Scraper())
    monkeypatch.setattr(deps, "get_gateway", lambda: gateway)
    monkeypatch.setattr(deps, "get_reranker", lambda: HeuristicReranker())
    deps.reset_pipelines()

    with TestClient(app) as client:
        yield client

    get_runtime_llm_store().reset()
    get_dream_rsi_store().reset()


class TestE2EFreeLLMDirectoriesAndPresets:
    def test_all_20_presets_and_free_llm_directories_exposed(self, e2e_client: TestClient) -> None:
        resp = e2e_client.get("/v1/llm/providers")
        assert resp.status_code == 200
        body = resp.json()

        # Verify directory references (nejib1/Free-LLM, awesome-freellm-apis, OpenRouter Free)
        dirs = body["free_directories"]
        dir_urls = {d["url"] for d in dirs}
        assert "https://github.com/nejib1/Free-LLM" in dir_urls
        assert "https://github.com/open-free-llm-api/awesome-freellm-apis" in dir_urls
        assert "https://openrouter.ai/openrouter/free" in dir_urls
        assert len(FREE_LLM_DIRECTORY_SOURCES) >= 3

        # Verify all 20 provider presets
        provider_ids = {p["id"] for p in body["providers"]}
        expected_20 = {
            "openrouter",
            "pollinations",
            "gemini",
            "groq",
            "nvidia_nim",
            "cerebras",
            "sambanova",
            "mistral",
            "github_models",
            "cloudflare",
            "cohere",
            "chutes",
            "siliconflow",
            "hyperbolic",
            "huggingface",
            "ollama",
            "custom",
            "openai",
            "anthropic",
            "none",
        }
        assert expected_20.issubset(provider_ids)


class TestE2EUniversalMultiDomainIntelligence:
    def test_domains_listing_and_flagship_financial_pilot(self, e2e_client: TestClient) -> None:
        resp = e2e_client.get("/v1/domains")
        assert resp.status_code == 200
        data = resp.json()
        assert data["flagship_pilot"] == "financial"
        assert data["total_domains"] >= 8
        domain_map = {d["id"]: d for d in data["domains"]}
        assert domain_map["financial"]["maturity"] == "flagship_pilot"
        assert "biomedical_health" in domain_map
        assert "technology_ai" in domain_map
        assert "academic_research" in domain_map
        assert "legal_macro_policy" in domain_map
        assert "eastern_metaphysics" in domain_map
        assert "news_geopolitics" in domain_map
        assert "general_discovery" in domain_map

    @pytest.mark.parametrize(
        ("query", "expected_domain"),
        [
            ("Phân tích BCTC VCB: tỷ lệ NIM, CASA, NPL và chỉ báo RSI của FPT", "financial"),
            (
                "Phân tích thử nghiệm lâm sàng RCT thuốc điều trị tiểu đường FDA PubMed",
                "biomedical_health",
            ),
            (
                "Kiến trúc hệ thống distributed consensus Raft và lỗ hổng bảo mật CVE OWASP",
                "technology_ai",
            ),
            ("Tác động lãi suất điều hành NHNN và tỷ giá USD/VND tới CPI", "legal_macro_policy"),
            (
                "Xem tuổi 1990 Canh Ngọ hợp hướng nhà nào theo Bát Trạch Cung Phi",
                "eastern_metaphysics",
            ),
            ("Tổng quan bài báo arXiv preprint về định lý hội tụ transformer", "academic_research"),
        ],
    )
    def test_domain_auto_detection_and_specialized_prompts(
        self, e2e_client: TestClient, query: str, expected_domain: str
    ) -> None:
        resp = e2e_client.post("/v1/domains/analyze", json={"query": query})
        assert resp.status_code == 200
        body = resp.json()
        assert body["detected_domain"] == expected_domain
        assert len(body["verification_rubric"]) >= 2
        assert len(body["preferred_engines"]) >= 2
        assert expected_domain in body["system_prompt"] or len(body["system_prompt"]) > 200


class TestE2EMultiEndpointCouncilAndDreamRSI:
    def test_council_deliberation_cross_review_and_dream_rsi_evolution(
        self, e2e_client: TestClient
    ) -> None:
        # 1. Update council endpoints via PATCH /v1/llm/council/endpoints
        patch_resp = e2e_client.patch(
            "/v1/llm/council/endpoints",
            json={
                "endpoints": [
                    "openrouter:openrouter/free",
                    "pollinations:openai",
                    "groq:llama-3.3-70b-versatile",
                ]
            },
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["count"] == 3

        # 2. Run standalone Council deliberation on Financial Flagship domain
        council_resp = e2e_client.post(
            "/v1/llm/council",
            json={
                "query": "Thẩm định pháp y BCTC VCB và xung lực kỹ thuật FPT",
                "domain": "financial",
                "chunks": [
                    {
                        "title": "BCTC VCB Q4",
                        "url": "https://finance.example.com/vcb",
                        "text": "VCB đạt NIM 3.25%, CASA 34.5%, NPL 0.96%, LLR 215%.",
                    },
                    {
                        "title": "Kỹ thuật FPT",
                        "url": "https://finance.example.com/fpt",
                        "text": "FPT tăng trưởng 24.8% YoY, RSI 63.5, giữ vững Fibonacci 61.8%.",
                    },
                ],
            },
        )
        assert council_resp.status_code == 200
        verdict = council_resp.json()
        assert verdict["domain"] == "financial"
        assert "3.25%" in verdict["answer_md"]
        assert "[1]" in verdict["answer_md"]
        assert len(verdict["citations"]) >= 1
        assert len(verdict["panelists"]) == 3
        assert len(verdict["cross_reviews"]) >= 1
        assert verdict["consensus_agreement_score"] > 0.0
        assert verdict["dream_rsi_policy"]["generation"] >= 1

        # 3. Check Dream-RSI state evolution
        rsi_resp = e2e_client.get("/v1/llm/dream-rsi/state", params={"domain": "financial"})
        assert rsi_resp.status_code == 200
        rsi_state = rsi_resp.json()
        assert rsi_state["generation"] >= 1
        assert rsi_state["replay_buffer_size"] >= 1
        assert len(rsi_state["endpoint_ranking"]) >= 3

        # 4. Reset Dream-RSI state
        reset_resp = e2e_client.post("/v1/llm/dream-rsi/reset")
        assert reset_resp.status_code == 200
        assert reset_resp.json()["total_trajectories"] == 0

    def test_council_graceful_degradation_when_one_endpoint_fails(
        self, e2e_client: TestClient, monkeypatch
    ) -> None:
        # Simulate 'groq' failing with 429 while 'openrouter' and 'pollinations' succeed
        partial_gw = _CouncilAwareFakeGateway(fail_providers={"groq"})
        monkeypatch.setattr(deps, "get_gateway", lambda: partial_gw)
        deps.reset_pipelines()

        resp = e2e_client.post(
            "/v1/llm/council",
            json={
                "query": "Phân tích VCB và FPT",
                "endpoints": [
                    "openrouter:openrouter/free",
                    "pollinations:openai",
                    "groq:llama-3.3-70b-versatile",
                ],
                "chunks": [
                    {
                        "title": "VCB Report",
                        "url": "https://finance.example.com/vcb",
                        "text": "VCB NIM 3.25%, CASA 34.5%, NPL 0.96%, LLR 215%.",
                    },
                    {
                        "title": "FPT Report",
                        "url": "https://finance.example.com/fpt",
                        "text": "FPT tăng trưởng 24.8% YoY.",
                    },
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        ok_panelists = [p for p in data["panelists"] if p["ok"]]
        failed_panelists = [p for p in data["panelists"] if not p["ok"]]
        assert len(ok_panelists) == 2
        assert len(failed_panelists) == 1
        assert failed_panelists[0]["provider"] == "groq"
        assert len(data["answer_md"]) > 30


class TestE2EAnswerPipelineWithCouncilAndStreaming:
    def test_answer_json_with_council_mode_enabled(self, e2e_client: TestClient) -> None:
        resp = e2e_client.post(
            "/v1/answer",
            json={
                "query": "Phân tích pháp y BCTC VCB và chỉ báo kỹ thuật FPT",
                "stream": False,
                "council_mode": True,
                "council_endpoints": [
                    "openrouter:openrouter/free",
                    "pollinations:openai",
                ],
                "domain": "financial",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in {"ok", "complete"}
        assert "[1]" in body["answer_md"]
        assert body["council"] is not None
        assert body["council"]["domain"] == "financial"
        assert len(body["council"]["panelists"]) == 2
        assert body["council"]["consensus_agreement_score"] > 0

    def test_answer_sse_stream_with_council_mode_enabled(self, e2e_client: TestClient) -> None:
        with e2e_client.stream(
            "POST",
            "/v1/answer",
            json={
                "query": "Đánh giá NIM của VCB và RSI của FPT",
                "stream": True,
                "council_mode": True,
                "council_endpoints": [
                    "openrouter:openrouter/free",
                    "pollinations:openai",
                ],
            },
        ) as resp:
            assert resp.status_code == 200
            raw_sse = "".join(resp.iter_text())

        assert "event: status" in raw_sse
        assert "event: sources" in raw_sse
        assert "event: token" in raw_sse
        assert "event: citations" in raw_sse
        assert "event: council" in raw_sse
        assert "event: done" in raw_sse


class TestE2EExternalSystemAndCoreEndpoints:
    def test_full_system_health_search_context_and_digests(self, e2e_client: TestClient) -> None:
        # 1. Health check
        h_resp = e2e_client.get("/healthz")
        assert h_resp.status_code == 200

        # 2. Engines listing
        e_resp = e2e_client.get("/v1/engines")
        assert e_resp.status_code == 200
        assert len(e_resp.json()) >= 1

        # 3. Raw multi-engine search
        s_resp = e2e_client.post("/v1/search", json={"query": "VCB NIM CASA", "k": 5})
        assert s_resp.status_code == 200
        s_body = s_resp.json()
        assert len(s_body["results"]) == 2

        # 4. Context-only RAG pack for external LLM systems + domain override
        c_resp = e2e_client.post(
            "/v1/context",
            json={"query": "Phân tích VCB và FPT", "k": 5, "budget": 4000, "domain": "financial"},
        )
        assert c_resp.status_code == 200
        c_body = c_resp.json()
        assert "[1]" in c_body["context_md"]
        assert len(c_body["chunks"]) >= 1

        # 5. Citation validation for external LLM output
        v_resp = e2e_client.post(
            "/v1/citations/validate",
            json={
                "answer_md": "VCB có CASA 34.5% [1] và nguồn ảo [88] bị cắt.",
                "chunks": c_body["chunks"],
            },
        )
        assert v_resp.status_code == 200
        v_body = v_resp.json()
        assert "[1]" in v_body["validated_answer_md"]
        assert "[88]" not in v_body["validated_answer_md"]
        assert v_body["invalid_markers"] == [88]

        # 6. Daily briefing topics
        t_resp = e2e_client.get("/v1/digests/topics")
        assert t_resp.status_code == 200
        assert len(t_resp.json()) >= 1
