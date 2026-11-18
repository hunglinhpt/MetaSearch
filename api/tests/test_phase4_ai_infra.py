"""Comprehensive unit & integration tests for the 4-repo AI infrastructure stack:

1. OmniRoute-inspired key validator + health-aware multi-LLM failover routing.
2. Headroom-inspired structural/semantic context compression + KV-cache prefix alignment.
3. One-Skill-to-Rule-Them-All unified skill orchestrator + bounded TaskObserver.
4. Claude-Mem-inspired episodic observation capture + 3-layer progressive disclosure + MCP tools.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.config import Settings, get_settings
from app.db.models import MemoryItem
from app.db.session import dispose_engine, get_engine
from app.extraction.chunker import Chunk
from app.extraction.scraper import PageContent, PageFailure
from app.llm.compressor import (
    CacheAligner,
    ContentRouter,
    SearchCompressor,
    SmartCrusher,
    compress_chunks,
)
from app.llm.gateway import LLMGateway, StreamDelta, Usage, llm_breakers
from app.llm.key_validator import (
    get_provider_health_tracker,
    validate_all_configured_providers,
    validate_key_format,
    validate_provider_key,
)
from app.llm.prompts import build_messages
from app.llm.providers import LLMOverride
from app.llm.router import LLMInferenceRouter, rank_providers
from app.main import create_app
from app.mcp import client as mcp_client
from app.orchestration.engine_store import InMemoryEngineStore
from app.orchestration.skill_orchestrator import get_task_observer, list_skills, select_skill
from app.retrieval import memory as mem_mod
from app.retrieval.models import EngineConfig, RawResult
from app.retrieval.reranker import HeuristicReranker

# ---------------------------------------------------------------------------
# 1. OmniRoute-inspired Key Validation & Intelligent Multi-Provider Routing
# ---------------------------------------------------------------------------


def test_validate_key_format_rules_and_placeholders() -> None:
    good_or = "sk-or-v1-" + ("a1b2c3d4" * 8)
    assert validate_key_format("openrouter", good_or).format_valid is True

    # Short / truncated OpenRouter key (25 chars like dummy placeholders)
    short_or = validate_key_format("openrouter", "sk-or-v1-1234567890abcdef")
    assert short_or.format_valid is False
    assert "too_short" in short_or.reason

    bad_prefix = validate_key_format("groq", "sk-not-a-groq-key-with-plenty-of-length-12345")
    assert bad_prefix.format_valid is False
    assert "expected_prefix" in bad_prefix.reason

    placeholder = validate_key_format("openai", "sk-your_api_key_placeholder_1234567890abcd")
    assert placeholder.format_valid is False
    assert "placeholder_marker" in placeholder.reason

    low_entropy = validate_key_format("gemini", "AIza" + "A" * 35)
    assert low_entropy.format_valid is False
    assert low_entropy.reason == "low_entropy_repeated_chars"

    keyless = validate_key_format("pollinations", "")
    assert keyless.configured is True
    assert keyless.format_valid is True


@pytest.mark.asyncio
async def test_validate_provider_key_live_probes_and_ssrf_guard() -> None:
    llm_breakers._breakers.clear()
    settings = Settings(
        llm_provider="groq",
        groq_api_key="gsk_" + "x9y8z7w6" * 6,
        openrouter_api_key="sk-or-v1-" + "m4n5b6v7" * 8,
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if "groq.com" in req.url.host:
            return httpx.Response(200, json={"data": [{"id": "llama-3.3-70b"}, {"id": "mixtral"}]})
        if "openrouter.ai" in req.url.host:
            return httpx.Response(
                401, json={"error": {"message": "Invalid credentials sk-secret-echo"}}
            )
        return httpx.Response(429, headers={"Retry-After": "12"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        res_ok = await validate_provider_key("groq", settings, client=client)
        assert res_ok.key_valid is True
        assert res_ok.reachable is True
        assert res_ok.models_count == 2
        assert "x9y8" not in res_ok.masked_key or "..." in res_ok.masked_key

        res_bad = await validate_provider_key("openrouter", settings, client=client)
        assert res_bad.key_valid is False
        assert res_bad.reachable is True
        assert "authentication_failed" in (res_bad.error or "")
        # Raw key & upstream body must never leak in error strings
        assert "m4n5b6v7" not in json.dumps(res_bad.to_dict())
        assert "sk-secret-echo" not in json.dumps(res_bad.to_dict())

        # SSRF-blocked metadata endpoint cannot even be called
        res_ssrf = await validate_provider_key(
            "custom",
            settings,
            request_override=LLMOverride(
                provider="custom", base_url="http://169.254.169.254/latest/meta-data"
            ),
            client=client,
        )
        assert res_ssrf.key_valid is False
        assert res_ssrf.reachable is False

        batch = await validate_all_configured_providers(settings, client=client)
        by_id = {r.provider: r for r in batch}
        assert by_id["groq"].key_valid is True
        assert by_id["openrouter"].key_valid is False

        # Allowlist enforcement blocks unapproved hosts cleanly when BYO is enabled
        restricted_no_byo = Settings(
            llm_provider="groq",
            groq_api_key="gsk_" + "x9y8z7w6" * 6,
            llm_byo_allowed_hosts="api.groq.com",
        )
        blocked_no_byo = await validate_provider_key(
            "custom",
            restricted_no_byo,
            request_override=LLMOverride(provider="custom", base_url="https://evil.example/v1"),
            client=client,
        )
        assert blocked_no_byo.key_valid is False
        assert "BYO LLM override is disabled" in (blocked_no_byo.error or "")

        restricted = Settings(
            llm_provider="groq",
            groq_api_key="gsk_" + "x9y8z7w6" * 6,
            llm_byo_enabled=True,
            llm_byo_allowed_hosts="api.groq.com",
        )
        blocked_host = await validate_provider_key(
            "custom",
            restricted,
            request_override=LLMOverride(provider="custom", base_url="https://evil.example/v1"),
            client=client,
        )
        assert blocked_host.key_valid is False
        assert blocked_host.reachable is False
        assert "not in allowed hosts" in (blocked_host.error or "")


@pytest.mark.asyncio
async def test_gateway_omniroute_auth_failover_and_ewma_ranking() -> None:
    llm_breakers._breakers.clear()
    settings = Settings(
        llm_provider="openrouter",
        openrouter_api_key="sk-or-v1-" + "deadkey1" * 7,
        groq_api_key="gsk_" + "livekey2" * 7,
        llm_fallback_chain="groq",
        llm_auto_fallback=True,
        llm_allow_keyless_fallback=False,
    )

    seen_hosts: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_hosts.append(req.url.host)
        if "openrouter.ai" in req.url.host:
            return httpx.Response(401, json={"error": "revoked"})
        lines = [
            b'data: {"choices":[{"delta":{"content":"Groq failover ok"}}]}\n\n',
            b"data: [DONE]\n\n",
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"".join(lines),
        )

    gw = LLMGateway(
        settings,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        use_runtime_store=False,
    )
    parts: list[str] = []
    async for delta in gw.stream([{"role": "user", "content": "test failover"}]):
        if delta.text:
            parts.append(delta.text)

    assert "".join(parts) == "Groq failover ok"
    assert len(seen_hosts) == 2
    assert "openrouter.ai" in seen_hosts[0]
    assert "groq.com" in seen_hosts[1]

    # Telemetry must mark openrouter invalid and rank groq cleanly ahead of openrouter
    snap = get_provider_health_tracker().snapshot()
    assert snap["openrouter"]["key_valid"] is False
    assert snap["groq"]["key_valid"] is True
    ranked = [r.provider for r in rank_providers(settings)]
    assert ranked.index("groq") < ranked.index("openrouter")


def test_router_privacy_guard_skips_pollinations_unless_opted_in() -> None:
    settings = Settings(
        llm_provider="groq",
        groq_api_key="gsk_" + "validkey" * 7,
        llm_auto_fallback=True,
        llm_allow_keyless_fallback=False,
    )
    router = LLMInferenceRouter(settings)
    primary = LLMGateway(settings, use_runtime_store=False).resolve_target()
    combo = router.build_combo(primary, primary.quick_model)
    assert [t.provider for t, _ in combo] == ["groq"]

    settings_optin = Settings(
        llm_provider="groq",
        groq_api_key="gsk_" + "validkey" * 7,
        llm_auto_fallback=True,
        llm_allow_keyless_fallback=True,
    )
    combo_optin = LLMInferenceRouter(settings_optin).build_combo(primary, primary.quick_model)
    assert "pollinations" in [t.provider for t, _ in combo_optin]

    # BYO custom credentials must NEVER fail over to other configured server providers
    byo_override = LLMOverride(provider="custom", base_url="https://internal.corp/v1", api_key="k")
    settings_byo = Settings(
        llm_provider="groq",
        groq_api_key="gsk_" + "validkey" * 7,
        gemini_api_key="AIza" + "SyTestKey" * 5,
        llm_byo_enabled=True,
        llm_byo_allowed_hosts="internal.corp",
    )
    combo_byo = LLMInferenceRouter(settings_byo).build_combo(
        primary, primary.quick_model, request_override=byo_override
    )
    assert [t.provider for t, _ in combo_byo] == ["groq"]


# ---------------------------------------------------------------------------
# 2. Headroom-inspired Context Compression & Cache Aligner
# ---------------------------------------------------------------------------


def test_headroom_content_router_crusher_and_extractive_fidelity() -> None:
    # Structured JSON pruning preserves queried record & compacts empty nulls
    json_payload = json.dumps(
        {
            "empty_meta": None,
            "nested_blank": {},
            "items": [
                {"id": i, "ticker": "OTHER" if i != 7 else "FPT", "roe": 12 + i} for i in range(25)
            ],
        }
    )
    assert ContentRouter.classify(json_payload) == "json"
    compacted = SmartCrusher.compact_json(json_payload, {"fpt", "roe"}, max_list_items=6)
    assert len(compacted) < len(json_payload)
    assert "FPT" in compacted
    assert "empty_meta" not in compacted

    # Markdown table pruning keeps totals and queried rows
    table_lines = ["| Ticker | ROE | Notes |", "| --- | --- | --- |"]
    for i in range(20):
        name = "VCB" if i == 11 else f"TICK{i}"
        table_lines.append(f"| {name} | {10 + i}% | Normal row |")
    table_lines.append("| Total Avg | 20.5% | Benchmark |")
    table_md = "\n".join(table_lines)
    assert ContentRouter.classify(table_md) == "markdown_table"
    comp_table = SmartCrusher.compact_markdown_table(table_md, {"vcb"}, max_rows=6)
    assert "| VCB |" in comp_table
    assert "| Total Avg |" in comp_table
    assert "rows omitted" in comp_table

    # Code blocks are recognized safely and untouched by destructive prose cropping
    code_chunk = "```python\ndef foo():\n    return 42\n```"
    assert ContentRouter.classify(code_chunk) == "code_log"

    # Extractive prose compression keeps high-signal verbatim sentences in original order
    prose = (
        "Accept all cookies\n"
        "FPT Corporation reported 2025 revenue of 62,849 billion VND, growing 19.4% YoY. "
        "The weather in Hanoi was cloudy yesterday afternoon for commuters. "
        "Many tourists enjoy walking around Hoan Kiem lake on weekends. "
        "Net profit reached 9,420 billion VND driven by global IT services."
    )
    c1 = Chunk(
        id="c1",
        document_url="https://example.com/1",
        position=1,
        heading_path="",
        content=prose,
        word_count=50,
    )
    c2 = Chunk(
        id="c2",
        document_url="https://example.com/2",
        position=2,
        heading_path="",
        content=prose
        + " Additional filler text repeated to trigger compression threshold cleanly.",
        word_count=60,
    )
    outcome = compress_chunks([c1, c2], query="FPT revenue net profit", budget=45, enabled=True)
    assert outcome.stats.compressed is True
    assert outcome.stats.saved_pct > 0
    joined = " ".join(c.content for c in outcome.chunks)
    assert "62,849" in joined
    assert "Accept all cookies" not in joined


def test_cache_aligner_prefix_hash_stable_across_variable_contexts() -> None:
    c1 = Chunk(
        id="1",
        document_url="https://a.com",
        position=1,
        heading_path="",
        content="Fact one 100%",
        word_count=3,
    )
    c2 = Chunk(
        id="2",
        document_url="https://b.com",
        position=1,
        heading_path="",
        content="Fact two 200%",
        word_count=3,
    )
    m1 = build_messages(
        "Query A?", [(1, c1)], skill_prompt="Active Skill X", memory_context="- Prior memory 1"
    )
    m2 = build_messages(
        "Query B?",
        [(2, c2)],
        skill_prompt="Active Skill X",
        memory_context="- Completely different",
    )
    assert CacheAligner.prefix_hash(m1) == CacheAligner.prefix_hash(m2)
    assert "Prior memory 1" in m1[1]["content"]
    assert "Prior memory 1" not in m1[0]["content"]


# ---------------------------------------------------------------------------
# 3. One-Skill-to-Rule-Them-All Unified Skill Orchestrator & TaskObserver
# ---------------------------------------------------------------------------


def test_skill_orchestrator_routing_and_observer() -> None:
    skills = list_skills()
    assert len(skills) >= 9
    fin = select_skill("Phân tích pháp y BCTC CAMELS VCB và chỉ số RSI 14")
    assert fin.skill_id == "financial_forensics"
    assert fin.compression_profile == "preserve_numbers"

    tech_deep = select_skill(
        "So sánh chi tiết ưu nhược điểm hai kiến trúc microservices và monolith trong hệ thống lớn"
    )
    assert tech_deep.complexity == "deep"
    assert tech_deep.skill_id == "technology_engineering"
    assert tech_deep.recommended_mode == "deep_research"

    deep_gen = select_skill(
        "Please compare historical rural development approaches across Southeast Asia "
        "regions in detail today"
    )
    assert deep_gen.complexity == "deep"
    assert deep_gen.skill_id == "deep_research_synthesis"
    assert deep_gen.recommended_mode == "deep_research"

    obs = get_task_observer()
    obs.record(
        skill_id=fin.skill_id,
        domain_id=fin.domain_id,
        complexity=fin.complexity,
        ok=True,
        latency_ms=120,
        compression_saved_pct=34.5,
        citations_count=3,
    )
    obs.record(
        skill_id=fin.skill_id,
        domain_id=fin.domain_id,
        complexity=fin.complexity,
        ok=False,
        latency_ms=250,
        invalid_markers_count=1,
        error_code="llm_error",
    )
    snap = obs.snapshot()
    assert snap["total_recorded"] == 2
    assert snap["skills"]["financial_forensics"]["friction_rate"] == 0.5


# ---------------------------------------------------------------------------
# 4. Claude-Mem Progressive Disclosure, Multi-Tenancy, Endpoints & MCP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_mem_progressive_disclosure_and_tenant_isolation(
    tmp_path, monkeypatch
) -> None:
    db_file = tmp_path / "mem_test.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file.as_posix()}")
    get_settings.cache_clear()
    await dispose_engine()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(MemoryItem.__table__.create)

    try:
        assert await mem_mod.record_observation(
            "owner-a",
            title="FPT Q4 2025 earnings analysis",
            summary="FPT revenue rose 19.4% to 62,849 billion VND driven by global IT outsourcing.",
            facts=["Revenue: 62,849B VND", "Net profit: 9,420B VND"],
            url="https://vietstock.vn/fpt",
            domain_id="financial",
            skill_id="financial_forensics",
            tags=["FPT", "earnings"],
        )
        assert await mem_mod.record_observation(
            "owner-a",
            title="VNIndex macro liquidity",
            summary="Margin lending rose 12% QoQ across major brokers.",
            url="https://cafef.vn/vni",
            domain_id="financial",
        )
        assert await mem_mod.record_observation(
            "owner-b",
            title="Secret B strategy on FPT",
            summary="Private owner-b alpha signal must never leak to owner-a.",
            url="https://private.b/fpt",
        )

        # Layer 1: compact index scoped strictly by owner-a
        idx_a = await mem_mod.search_memory_index("owner-a", "FPT earnings", kind="observation")
        assert len(idx_a) >= 1
        assert all("owner-b" not in x["title"] and "Secret B" not in x["title"] for x in idx_a)
        anchor_id = idx_a[0]["id"]
        assert "facts" not in idx_a[0]  # Layer 1 stays compact!

        # Layer 2: chronological timeline around anchor_id
        tl_a = await mem_mod.memory_timeline("owner-a", anchor_id=anchor_id, before=2, after=2)
        assert tl_a["anchor_found"] is True
        assert any(i["is_anchor"] for i in tl_a["items"])

        # Tenant isolation check: owner-b cannot view owner-a's timeline anchor or full observation
        tl_b = await mem_mod.memory_timeline("owner-b", anchor_id=anchor_id, before=2, after=2)
        assert tl_b["anchor_found"] is False
        assert tl_b["items"] == []

        obs_a = await mem_mod.get_memory_observations("owner-a", ids=[anchor_id])
        assert len(obs_a) == 1
        assert "Revenue: 62,849B VND" in obs_a[0]["facts"]

        obs_b_steal = await mem_mod.get_memory_observations("owner-b", ids=[anchor_id])
        assert obs_b_steal == []

        recalled = await mem_mod.recall_context_for_query("owner-a", "FPT revenue", max_tokens=200)
        assert "FPT" in recalled
        assert "Secret B" not in recalled
    finally:
        await dispose_engine()


@pytest.mark.asyncio
async def test_http_endpoints_validate_health_skills_and_progressive_memory(
    tmp_path, monkeypatch
) -> None:
    db_file = tmp_path / "api_p4.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_" + "abcd1234" * 6)
    get_settings.cache_clear()
    await dispose_engine()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(MemoryItem.__table__.create)
    deps.reset_pipelines()

    app = create_app()
    with TestClient(app) as client:
        # 1. Dry-run key validation endpoint
        val_resp = client.post("/v1/llm/validate", json={"dry_run": True})
        assert val_resp.status_code == 200
        val_data = val_resp.json()
        assert val_data["dry_run"] is True
        groq_item = next(r for r in val_data["results"] if r["provider"] == "groq")
        assert groq_item["format_valid"] is True
        assert "..." in groq_item["masked_key"]

        # 2. Health & ranking endpoint
        h_resp = client.get("/v1/llm/health?task=answer")
        assert h_resp.status_code == 200
        assert any(p["provider"] == "groq" for p in h_resp.json()["ranked_providers"])

        # 3. Skills discovery + routing preview + observer ops
        s_resp = client.get("/v1/skills?q=Phân tích BCTC VCB")
        assert s_resp.status_code == 200
        s_json = s_resp.json()
        assert s_json["total"] >= 9
        assert s_json["routed"]["skill_id"] == "financial_forensics"

        ops_resp = client.get("/v1/ops/skills")
        assert ops_resp.status_code == 200
        assert "skills" in ops_resp.json()

        # 4. Memory progressive disclosure routes end-to-end
        add_resp = client.post(
            "/v1/memory",
            json={
                "kind": "observation",
                "text": "VIC liquidity event\nFree float shrank after tender offer.",
                "url": "https://vnexpress.net/vic",
                "weight": 1.0,
                "payload": {"title": "VIC liquidity event", "tags": ["VIC", "liquidity"]},
            },
        )
        assert add_resp.status_code == 201
        obs_id = add_resp.json()["id"]

        srch = client.get("/v1/memory/search?q=VIC&kind=observation")
        assert srch.status_code == 200
        assert len(srch.json()["items"]) == 1
        assert srch.json()["items"][0]["id"] == obs_id

        tl = client.get(f"/v1/memory/timeline?anchor_id={obs_id}")
        assert tl.status_code == 200
        assert tl.json()["anchor_found"] is True

        full = client.post("/v1/memory/observations", json={"ids": [obs_id]})
        assert full.status_code == 200
        assert len(full.json()["items"]) == 1
        assert full.json()["items"][0]["id"] == obs_id

        # 5. Auth check: POST /v1/llm/validate requires admin when AUTH_ENABLED=true
        monkeypatch.setenv("AUTH_ENABLED", "true")
        get_settings.cache_clear()
        deps.reset_pipelines()
        app_auth = create_app()
        with TestClient(app_auth) as auth_client:
            denied = auth_client.post("/v1/llm/validate", json={"dry_run": True})
            assert denied.status_code in (401, 403)
    await dispose_engine()


def test_mcp_progressive_memory_tools_dispatch(monkeypatch) -> None:
    calls: list[tuple[str, str, dict | None]] = []

    async def fake_call(method: str, path: str, payload=None):
        calls.append((method, path, payload))
        return {"items": [{"id": "m1", "title": "test"}], "anchor_found": True}

    monkeypatch.setattr(mcp_client, "_call", fake_call)
    res_search = json.loads(mcp_client.run_tool("memory_search", {"query": "FPT", "limit": 5}))
    res_tl = json.loads(mcp_client.run_tool("memory_timeline", {"anchor_id": "m1"}))
    res_obs = json.loads(mcp_client.run_tool("memory_get_observations", {"ids": ["m1"]}))

    assert res_search["items"][0]["id"] == "m1"
    assert res_tl["anchor_found"] is True
    assert res_obs["items"][0]["id"] == "m1"
    assert calls[0][0] == "GET" and "/v1/memory/search" in calls[0][1]
    assert calls[1][0] == "GET" and "anchor_id=m1" in calls[1][1]
    assert calls[2] == ("POST", "/v1/memory/observations", {"ids": ["m1"]})


def test_search_compressor_verbatim_sentences() -> None:
    prose = (
        "Cookie preferences allow basic analytics on this website.\n"
        "FPT reported FY2025 revenue of 62,849 billion VND and net profit rose 19.4% YoY.\n"
        "Visitors often browse related lifestyle stories during holidays.\n"
        "AI cloud contracts reached 180 million USD backlog across Japan and APAC.\n"
        "Click here to view general archive photos from last summer."
    )
    out = SearchCompressor.compress(
        prose,
        query_tokens={"fpt", "revenue", "profit", "ai"},
        target_units=30,
        count=lambda s: len(s.split()),
    )
    assert "62,849 billion VND" in out
    assert "180 million USD" in out
    assert len(out.split()) < len(prose.split())


@pytest.mark.asyncio
async def test_e2e_context_and_answer_with_skill_compression_and_task_observer(
    monkeypatch, tmp_path
) -> None:
    db_file = tmp_path / f"e2e-{uuid.uuid4().hex}.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("ANSWER_CONTEXT_BUDGET", "200")
    get_settings.cache_clear()
    await dispose_engine()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(MemoryItem.__table__.create)

    class _Adapter:
        id = "e2e_fake"

        async def search(self, query, **kwargs):
            return [
                RawResult(
                    url="https://finance.example.com/vcb",
                    title="VCB Financial Analysis",
                    snippet="VCB Q4 ROE 21.3% NIM 3.1%",
                    adapter=self.id,
                    source_engines=["viet_stock"],
                    rank=0,
                ),
                RawResult(
                    url="https://news.example.com/vcb",
                    title="VCB dividend outlook",
                    snippet="VCB retains earnings for Tier-1 ratio",
                    adapter=self.id,
                    source_engines=["google_news"],
                    rank=1,
                ),
            ]

    long_md = (
        "# VCB Financial Report\n\n"
        + "Accept all cookies.\n"
        + "VCB FY2025 ROE reached 21.3% and NIM held at 3.1% with CASA at 35.8%.\n"
        + ("General investor commentary without concrete metrics. " * 60)
    )
    pages = {
        "https://finance.example.com/vcb": PageContent(
            url="https://finance.example.com/vcb",
            final_url="https://finance.example.com/vcb",
            title="VCB Financial Analysis",
            markdown=long_md,
            extractor_used="trafilatura",
            fetched_at=datetime.now(UTC),
        ),
        "https://news.example.com/vcb": PageContent(
            url="https://news.example.com/vcb",
            final_url="https://news.example.com/vcb",
            title="VCB dividend outlook",
            markdown="# VCB Capital Plan\n\nVCB maintains CAR at 11.9% and NPL below 1.1%.",
            extractor_used="trafilatura",
            fetched_at=datetime.now(UTC),
        ),
    }

    class _Scraper:
        async def scrape_many(self, urls, **kwargs):
            return [
                pages.get(u) or PageFailure(url=u, status="failed", error="missing") for u in urls
            ]

    class _Cache:
        enabled = False

        async def get_json(self, key):
            return None

        async def set_json(self, key, value, *, ttl_s):
            return None

        async def has_flag(self, key):
            return False

        async def set_flag(self, key, *, ttl_s):
            return None

    class _Gateway:
        provider = "groq"

        async def stream(self, messages, *, model, max_tokens=None, **kwargs):
            yield StreamDelta(text="VCB ROE is 21.3% [1] with CAR 11.9% [2].")
            yield StreamDelta(usage=Usage(input_tokens=80, output_tokens=16))

    adapter = _Adapter()
    store = InMemoryEngineStore(
        {adapter.id: EngineConfig(id=adapter.id, name="E2E Fake", category="general", enabled=True)}
    )
    monkeypatch.setattr(deps, "get_engine_store", lambda: store)
    monkeypatch.setattr(deps, "build_adapters", lambda settings: {adapter.id: adapter})
    monkeypatch.setattr(deps, "get_cache", lambda: _Cache())
    monkeypatch.setattr(deps, "get_scraper", lambda: _Scraper())
    monkeypatch.setattr(deps, "get_gateway", lambda: _Gateway())
    monkeypatch.setattr(deps, "get_reranker", lambda: HeuristicReranker())
    deps.reset_pipelines()

    app = create_app()
    with TestClient(app) as client:
        ctx = client.post(
            "/v1/context",
            json={
                "query": "Phan tich BCTC ROE NIM co phieu VCB",
                "budget": 200,
                "include": ["chunks", "sources", "compression", "skill"],
            },
        )
        assert ctx.status_code == 200
        ctx_body = ctx.json()
        assert ctx_body["skill"]["skill_id"] == "financial_forensics"
        assert ctx_body["compression"]["chunks_in"] >= 1
        assert ctx_body["compression"]["compressed"] is True

        ans = client.post("/v1/answer", json={"query": "Phan tich BCTC ROE co phieu VCB"})
        assert ans.status_code == 200
        ans_body = ans.json()
        assert "[1]" in ans_body["answer_md"]
        assert ans_body["skill"]["skill_id"] == "financial_forensics"
        assert ans_body["compression"]["saved_units"] > 0

        ops = client.get("/v1/ops/skills")
        assert ops.status_code == 200
        assert ops.json()["total_recorded"] >= 1
        assert ops.json()["skills"]["financial_forensics"]["runs"] >= 1
    await dispose_engine()


@pytest.mark.asyncio
async def test_phase5_profile_override_isolation_and_timeline(monkeypatch, tmp_path) -> None:
    from datetime import timedelta

    from app.db.session import get_sessionmaker
    from app.retrieval.memory import memory_timeline

    # 1. Runtime override scoped strictly to its provider (no credential leak to others)
    s = Settings(llm_provider="groq")
    rt_ov = LLMOverride(provider="groq", api_key="gsk_" + "z9y8x7w6" * 6)
    seen_headers: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen_headers[req.url.host] = req.headers.get("authorization", "")
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        res = await validate_all_configured_providers(
            s, runtime_override=rt_ov, providers=["groq", "openai"], client=client
        )
        by_id = {r.provider: r for r in res}
        assert by_id["groq"].key_valid is True
        assert "api.openai.com" not in seen_headers  # openai unconfigured, never sent groq key!

    # 2. Compression profile preserves more financial table rows for preserve_numbers vs compact
    rows = "\n".join(f"| Q{i} 2025 | {100 + i}% | {i * 10} |" for i in range(1, 21))
    table_md = f"| Period | Margin | Rev |\n| --- | --- | --- |\n{rows}"
    chunks = [
        Chunk(
            id="t1",
            document_url="https://ex.com/t1",
            position=1,
            heading_path="",
            content=table_md,
            word_count=len(table_md.split()),
            source_title="T1",
        ),
        Chunk(
            id="t2",
            document_url="https://ex.com/t2",
            position=2,
            heading_path="",
            content="Extra context",
            word_count=2,
            source_title="T2",
        ),
    ]
    out_num = compress_chunks(chunks, query="Margin Q5", budget=5000, profile="preserve_numbers")
    out_cmp = compress_chunks(chunks, query="Margin Q5", budget=5000, profile="compact")
    assert out_num.chunks[0].content.count("| Q") > out_cmp.chunks[0].content.count("| Q")

    # 3. Router ranks trial_credits properly and marks unconfigured custom format_valid=False
    ranked = rank_providers(Settings(llm_provider="none"), include_unconfigured=True)
    by_rank = {r.provider: r for r in ranked}
    assert by_rank["custom"].format_valid is False
    assert by_rank["nvidia_nim"].format_valid is False
    assert by_rank["nvidia_nim"].free_tier_type == "trial_credits"
    assert by_rank["pollinations"].score < by_rank["nvidia_nim"].score

    # 4. memory_timeline finds items > 500 cleanly without full-table cap truncation
    db_file = tmp_path / f"tl-{uuid.uuid4().hex}.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file.as_posix()}")
    get_settings.cache_clear()
    await dispose_engine()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(MemoryItem.__table__.create)

    base_ts = datetime(2026, 11, 15, 0, 0, 0, tzinfo=UTC)
    items = [
        MemoryItem(
            id=uuid.uuid4(),
            api_key_id="tl-owner",
            kind="observation",
            text=f"Obs {i}",
            weight=1.0,
            payload={"title": f"Obs {i}", "target": f"tl-{i}"},
            created_at=base_ts + timedelta(seconds=i),
        )
        for i in range(510)
    ]
    async with get_sessionmaker()() as session:
        session.add_all(items)
        await session.commit()

    latest = await memory_timeline("tl-owner", anchor_id=None, before=1, after=1)
    assert latest["anchor_found"] is False
    assert [it["title"] for it in latest["items"]] == ["Obs 507", "Obs 508", "Obs 509"]

    anchored = await memory_timeline("tl-owner", anchor_id=str(items[505].id), before=1, after=1)
    assert anchored["anchor_found"] is True
    assert [it["title"] for it in anchored["items"]] == ["Obs 504", "Obs 505", "Obs 506"]
    await dispose_engine()


@pytest.fixture(autouse=True)
def _open_llm_endpoint_policy(monkeypatch):
    """docs/30 Part 1: gateway-mechanics tests exercise breaker/retry/stream
    behavior over non-trusted fixture endpoints — run with the privacy
    policy open (policy itself is covered in test_v30_endpoint_policy_and_wire)."""

    monkeypatch.setattr("app.llm.gateway.llm_endpoint_is_trusted", lambda url: True)
