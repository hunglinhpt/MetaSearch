"""docs/30 — 5-step transformation verification tests.

Step 1 LLM endpoint allowlist · Step 2 sliding-window expose · Step 3
news-wire bilingual entity tagging · Step 4 FA expansion · Step 5
sentiment isolation (already pinned in test_v29_production_defects).
"""

from __future__ import annotations

import pytest

from app.llm.endpoint_policy import (
    enforce_llm_endpoint_policy,
    llm_endpoint_is_trusted,
    sweep_configured_endpoints,
)
from app.retrieval.aliases import reset_alias_index, resolve_alias_tickers

# ---------------------------------------------------------------------------
# Step 1 — LLM Endpoint Privacy Policy
# ---------------------------------------------------------------------------


class TestLLMEndpointPolicy:
    def test_enterprise_providers_trusted(self) -> None:
        assert llm_endpoint_is_trusted("https://api.anthropic.com/v1")
        assert llm_endpoint_is_trusted("https://api.openai.com/v1")
        assert llm_endpoint_is_trusted("https://generativelanguage.googleapis.com/v1beta")
        assert llm_endpoint_is_trusted("https://aiplatform.googleapis.com/v1")

    def test_local_inference_trusted(self) -> None:
        assert llm_endpoint_is_trusted("http://localhost:11434")
        assert llm_endpoint_is_trusted("http://127.0.0.1:8000/v1")
        assert llm_endpoint_is_trusted("http://ollama-box.lan:11434")
        assert llm_endpoint_is_trusted("http://vllm.internal:8000/v1")

    def test_unknown_providers_untrusted(self) -> None:
        assert not llm_endpoint_is_trusted("https://openrouter.ai/api/v1")
        assert not llm_endpoint_is_trusted("https://api.pollinations.ai")
        assert not llm_endpoint_is_trusted("https://api.groq.com/openai/v1")

    def test_suffix_spoof_blocked(self) -> None:
        assert not llm_endpoint_is_trusted("https://api.openai.com.evil.io/v1")
        assert not llm_endpoint_is_trusted("https://api.anthropic.com.attacker.tld")

    def test_enforce_blocks_untrusted_at_runtime(self) -> None:
        with pytest.raises(Exception, match="trusted provider allowlist"):
            enforce_llm_endpoint_policy("https://openrouter.ai/api/v1")
        assert (
            enforce_llm_endpoint_policy("https://api.anthropic.com/v1")
            == "https://api.anthropic.com/v1"
        )
        # operator override restores legacy behavior
        assert (
            enforce_llm_endpoint_policy("https://openrouter.ai/api/v1", policy="open")
            == "https://openrouter.ai/api/v1"
        )

    def test_startup_sweep_lists_violations(self) -> None:
        clean = sweep_configured_endpoints(["https://api.openai.com/v1", "http://localhost:11434"])
        assert clean == []
        dirty = sweep_configured_endpoints(
            ["https://api.openai.com/v1", "https://openrouter.ai/api/v1"]
        )
        assert dirty == ["https://openrouter.ai/api/v1"]

    def test_settings_flag_exists_and_defaults_trusted(self) -> None:
        from app.config import get_settings

        assert get_settings().llm_endpoint_policy == "trusted_only"


# ---------------------------------------------------------------------------
# Step 2 — sliding-window health exposed on /v1/engines/health
# ---------------------------------------------------------------------------


class TestSlidingWindowExpose:
    async def test_recent_windows_counts_and_alerts(self) -> None:
        from app.orchestration.engine_store import InMemoryEngineStore
        from app.retrieval.models import EngineConfig

        store = InMemoryEngineStore({"ddg": EngineConfig(id="ddg", name="DDG", category="general")})
        for i in range(10):
            ok = i >= 4  # first 4 fail, last 6 succeed
            await store.record_result(
                "ddg", ok=ok, latency_ms=10, outcome="ok" if ok else "blocked"
            )
        windows = await store.recent_windows()
        w = windows["ddg"]
        assert w["total"] == 10
        assert w["failures"] == 4
        assert w["failure_rate"] == 0.4
        assert w["empty_count"] == 0
        assert w["degraded"] is False  # below the 0.5 rate threshold
        assert len(w["outcomes"]) == 10

    async def test_db_store_delegates_recent_windows(self) -> None:
        from app.orchestration.engine_store import (
            DbEngineStore,
            InMemoryEngineStore,
        )
        from app.retrieval.models import EngineConfig

        fallback = InMemoryEngineStore(
            {"searxng": EngineConfig(id="searxng", name="SearXNG", category="general")}
        )
        store = DbEngineStore(refresh_s=0, fallback=fallback)
        await store.record_result("searxng", ok=False, latency_ms=5, outcome="timeout")
        windows = await store.recent_windows()
        assert windows["searxng"]["failures"] == 1


# ---------------------------------------------------------------------------
# Step 3 — news-wire bilingual entity tagging (Stage B alias resolver)
# ---------------------------------------------------------------------------


class TestWireEntityTagging:
    def test_brand_aliases_resolve(self) -> None:
        reset_alias_index()
        assert resolve_alias_tickers("Vinamilk chi cổ tức tiền mặt cao") == ["VNM"]
        assert resolve_alias_tickers("Hoàng Quân Địa ốc thành công lớn") == ["HQC"]
        assert resolve_alias_tickers("Thép Hòa Phát tăng sản lượng") == ["HPG"]

    def test_macro_text_yields_no_aliases(self) -> None:
        reset_alias_index()
        assert resolve_alias_tickers("Thị trường chứng khoán phiên sáng tăng điểm") == []

    def test_extractor_merges_aliases(self) -> None:
        from app.retrieval.events import extract_tickers_from_text

        assert extract_tickers_from_text("Vinamilk chi cổ tức tiền mặt cao") == ["VNM"]
        assert extract_tickers_from_text("Hoàng Quân Địa ốc thành công lớn") == ["HQC"]
        # cashtag/exchange patterns still work (Stage A parity)
        assert "FPT" in extract_tickers_from_text("(HOSE: FPT) khối ngoại mua ròng")

    def test_allowed_symbols_contract_holds_for_aliases(self) -> None:
        from app.retrieval.events import extract_tickers_from_text

        text = "Vinamilk và Hoàng Quân Địa ốc cùng tăng"
        got = extract_tickers_from_text(text, allowed_symbols={"VNM"})
        assert got == ["VNM"]  # HQC filtered by the caller's allowlist


# ---------------------------------------------------------------------------
# Step 4 — FA expansion (Simplize registered; live fundamentals wired)
# ---------------------------------------------------------------------------


class TestFAExpansion:
    def test_simplize_registered_with_priority_below_fireant(self) -> None:
        from app.retrieval.fa_sources import build_default_aggregator

        sources = {s.source_id: s.priority for s in build_default_aggregator()._sources}
        assert sources["simplize"] == 0.6
        assert sources["fireant"] > sources["simplize"]  # token-gated wins if live
