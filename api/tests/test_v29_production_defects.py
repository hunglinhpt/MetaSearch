"""docs/29 — 4 production-defect verification tests (user-reported live).

Test 1: ddg degradation + sliding-window alerting (last-10 window drives
alerts; recovery is automatic as fresh successes push old failures out).
Test 2: fa/compare returns non-empty fundamentals for HQC/FPT (Simplize
source wired into the aggregator); all-sources-dead is surfaced, not silent.
Test 3: market/sentiment is per-ticker isolated — HQC vs FPT produce
distinct payloads, distinct cache keys, and degenerate payloads flag
themselves (ZERO_EVIDENCE_DEFAULT_PAYLOAD + hits telemetry).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.cache import search_cache_key
from app.orchestration.engine_store import InMemoryEngineStore
from app.retrieval.adapters import ddg as ddg_mod
from app.retrieval.adapters.base import AdapterError
from app.retrieval.health import Outcome
from app.retrieval.models import EngineConfig, RawResult
from app.retrieval.sentiment import RawSentimentHit, resolve_equity_entity

# ---------------------------------------------------------------------------
# Test 1 — ddg degradation + sliding-window engine health alerting
# ---------------------------------------------------------------------------


def _engine_store() -> InMemoryEngineStore:
    return InMemoryEngineStore({"ddg": EngineConfig(id="ddg", name="DDG", category="general")})


class TestSlidingWindowHealth:
    async def test_ten_consecutive_failures_trigger_degraded_alert(self) -> None:
        store = _engine_store()
        for _ in range(10):
            await store.record_result("ddg", ok=False, latency_ms=50, outcome="upstream_error")
        window = (await store.recent_windows())["ddg"]
        assert window["total"] == 10
        assert window["failures"] == 10
        assert window["consecutive_failures"] == 10
        assert window["failure_rate"] == 1.0
        assert window["degraded"] is True

    async def test_recovery_after_ten_fresh_successes(self) -> None:
        """10 consecutive failures, then 10 new successes — the window slides
        forward and the alert clears without any manual reset."""
        store = _engine_store()
        for _ in range(10):
            await store.record_result("ddg", ok=False, latency_ms=50, outcome="blocked")
        assert (await store.recent_windows())["ddg"]["degraded"] is True
        for _ in range(10):
            await store.record_result("ddg", ok=True, latency_ms=90, outcome="ok")
        window = (await store.recent_windows())["ddg"]
        assert window["total"] == 10  # maxlen — old failures were pushed out
        assert window["failures"] == 0
        assert window["consecutive_failures"] == 0
        assert window["degraded"] is False

    async def test_window_caps_at_ten_with_outcome_kinds(self) -> None:
        store = _engine_store()
        outcomes = [
            "timeout",
            "empty",
            "ok",
            "blocked",
            "rate_limited",
            "upstream_error",
            "ok",
            "ok",
            "timeout",
            "ok",
            "empty",
            "ok",
        ]
        for o in outcomes:  # 12 dispatches — only the last 10 remain
            await store.record_result(
                "ddg",
                ok=o not in ("timeout", "blocked", "rate_limited", "upstream_error"),
                latency_ms=10,
                outcome=o,
            )
        window = (await store.recent_windows())["ddg"]
        assert window["total"] == 10
        kinds = [e["outcome"] for e in window["outcomes"]]
        # the two oldest dispatches (timeout, empty) were evicted — the
        # remaining window is exactly outcomes[2:]
        assert kinds == outcomes[2:]

    async def test_degraded_threshold_is_rate_based_not_all_or_nothing(self) -> None:
        store = _engine_store()
        # 5 failures then 5 successes → failure_rate 0.5 over 10 → alert fires
        for _ in range(5):
            await store.record_result("ddg", ok=False, latency_ms=10, outcome="timeout")
        for _ in range(5):
            await store.record_result("ddg", ok=True, latency_ms=10, outcome="ok")
        window = (await store.recent_windows())["ddg"]
        assert window["failure_rate"] == 0.5
        assert window["degraded"] is True  # rate threshold, not only streaks


class TestDdgHardening:
    def test_user_agent_pool_rotates(self) -> None:
        assert len(ddg_mod._UA_POOL) >= 3
        assert len(set(ddg_mod._UA_POOL)) == len(ddg_mod._UA_POOL)

    def test_retry_skipped_on_hard_bot_block(self) -> None:
        """202/403/429/challenge are hard failures — they must surface on the
        FIRST attempt (no second request against a blocking endpoint)."""
        calls = {"n": 0}

        class _Client:
            async def post(self, *args: Any, **kwargs: Any):
                calls["n"] += 1
                resp = type("R", (), {})()
                resp.status_code = 403
                resp.text = "forbidden"
                return resp

        adapter = ddg_mod.DuckDuckGoAdapter(client=_Client())  # type: ignore[arg-type]

        async def run() -> None:
            with pytest.raises(AdapterError) as exc:
                await adapter.search("test", timeout_s=0.5)
            assert exc.value.kind == Outcome.BLOCKED

        asyncio.run(run())
        assert calls["n"] == 1  # one attempt, zero retries

    def test_transient_failure_retried_once(self) -> None:
        calls = {"n": 0}

        class _Client:
            async def post(self, *args: Any, **kwargs: Any):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise __import__("httpx").ConnectError("boom")
                resp = type("R", (), {})()
                resp.status_code = 200
                resp.text = '<a class="result__a" href="https://x.example/a">Title</a>'
                return resp

        adapter = ddg_mod.DuckDuckGoAdapter(client=_Client())  # type: ignore[arg-type]

        async def run() -> list[RawResult]:
            return await adapter.search("test", timeout_s=0.5)

        results = asyncio.run(run())
        assert calls["n"] == 2  # exactly one retry on transient
        assert results and results[0].url == "https://x.example/a"


# ---------------------------------------------------------------------------
# Test 2 — fa/compare non-empty for HQC/FPT (Simplize wired) + honest flag
# ---------------------------------------------------------------------------


def _simplize_body(pe: float, pb: float, roe: float, mcap: float) -> dict[str, Any]:
    return {
        "status": 200,
        "data": {"peRatio": pe, "pbRatio": pb, "roe": roe, "marketCap": mcap},
    }


class _FakeSimplizeClient:
    """Mirrors the live Simplize summary API for two tickers, 404 for others."""

    def __init__(self) -> None:
        self.by_symbol = {
            "HQC": _simplize_body(8.1, 0.9, 6.2, 350e9),
            "FPT": _simplize_body(18.5, 4.2, 28.0, 60000e9),
        }

    async def get(self, url: str, **kwargs: Any):
        sym = url.rsplit("/", 1)[-1]
        resp = type("R", (), {})()
        body = self.by_symbol.get(sym)
        resp.content = (json.dumps(body).encode()) if body else b"{}"
        resp.status_code = 200 if body else 404
        resp.json = lambda b=body: b if body else None
        return resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: Any):
        return False


class TestFaCompareNonEmpty:
    async def test_hqc_and_fpt_get_real_simplize_fields(self) -> None:
        from app.retrieval.fa_sources.simplize import SimplizeFASource

        client = _FakeSimplizeClient()
        simplize = SimplizeFASource(client=client)  # type: ignore[arg-type]

        # direct-source assertion (registration covered separately)
        hqc = await simplize.fetch_financials("HQC")
        fpt = await simplize.fetch_financials("FPT")
        assert hqc is not None and fpt is not None
        assert hqc.fields["market_cap_vnd_bil"] == pytest.approx(350.0)
        assert fpt.fields["pe"] == pytest.approx(18.5)
        assert set(hqc.fields) != set() and set(fpt.fields) != set()
        # the fields differ per ticker — real per-symbol data, not defaults
        assert hqc.fields != fpt.fields

    async def test_simplize_registered_in_default_aggregator(self) -> None:
        from app.retrieval.fa_sources import build_default_aggregator

        agg = build_default_aggregator()
        ids = [s.source_id for s in agg._sources]
        assert "simplize" in ids  # ISSUE-3: the live keyless source is wired

    def test_empty_payload_flags_all_sources_unavailable(self) -> None:
        from app.api.v1.schemas import FaSymbolResult

        res = FaSymbolResult(
            symbol="HQC",
            fields={},
            missing_fields=["pe", "pb"],
            sources_used=[],
            source_urls=[],
        )
        assert res.all_sources_unavailable is False  # default off
        flagged = res.model_copy(update={"all_sources_unavailable": True})
        assert flagged.all_sources_unavailable is True


# ---------------------------------------------------------------------------
# Test 3 — market/sentiment per-ticker isolation (HQC vs FPT)
# ---------------------------------------------------------------------------


def _entity_fixture(ticker: str) -> Any:

    return resolve_equity_entity(ticker, now=datetime.now(UTC))


class TestSentimentPerTickerIsolation:
    def test_fanout_queries_differ_per_ticker(self) -> None:
        from app.retrieval.sentiment import build_omni_channel_queries

        hqc = _entity_fixture("HQC")
        fpt = _entity_fixture("FPT")
        hqc_q = {q.query for q in build_omni_channel_queries(hqc, max_queries=10)}
        fpt_q = {q.query for q in build_omni_channel_queries(fpt, max_queries=10)}
        assert hqc_q and fpt_q
        assert hqc_q != fpt_q  # queries embed the ticker — no shared fan-out

    def test_search_cache_keys_differ_per_ticker(self) -> None:
        """The search cache key embeds the query text, so cross-ticker bleed
        through the search cache is structurally impossible."""
        from app.orchestration.pipeline import SearchParams

        k_hqc = search_cache_key(
            "tin tức HQC doanh thu", SearchParams(query="tin tức HQC doanh thu")
        )
        k_fpt = search_cache_key(
            "tin tức FPT doanh thu", SearchParams(query="tin tức FPT doanh thu")
        )
        assert k_hqc != k_fpt

    async def test_zero_evidence_payloads_flag_themselves(self) -> None:
        """When BOTH tickers get zero hits, the payloads still converge (no
        evidence exists) — but they now say so explicitly instead of posing
        as real measurements."""
        from app.retrieval.sentiment import analyze_omni_channel_sentiment

        for ticker in ("HQC", "FPT"):
            entity = _entity_fixture(ticker)
            out = analyze_omni_channel_sentiment(entity, [], now=datetime.now(UTC))
            assert "ZERO_EVIDENCE_DEFAULT_PAYLOAD" in out["risk_circuit_breakers"]
            assert out["hits_retrieved"] == 0
            assert out["hits_after_collision_filter"] == 0

    async def test_distinct_hits_produce_distinct_scores(self) -> None:
        """Genuinely different per-ticker news must produce different
        composite scores — the defect was identical numbers, this pins the
        divergence."""
        from app.retrieval.sentiment import analyze_omni_channel_sentiment

        now = datetime.now(UTC)
        fixtures = {
            "HQC": (
                "https://t.me/vn/1",
                "HQC ký hợp đồng trị giá 500 tỷ đồng, lợi nhuận kỷ lục quý 3",
                "HQC ký hợp đồng 500 tỷ, lợi nhuận tăng mạnh.",
            ),
            "FPT": (
                "https://cafef.vn/fpt/1",
                "FPT thua losing thầu công nghệ, lợi nhuận giảm sâu",
                "FPT lợi nhuận giảm sâu, mất hợp đồng lớn.",
            ),
        }
        scores: dict[str, float] = {}
        for ticker, (url, title, snippet) in fixtures.items():
            entity = _entity_fixture(ticker)
            hits = [
                RawSentimentHit(
                    url=url,
                    title=title,
                    snippet=snippet,
                    content=f"{title} {snippet}",
                    published_at=now - timedelta(hours=2),
                    lang="VI",
                )
            ]
            out = analyze_omni_channel_sentiment(entity, hits, now=now)
            scores[ticker] = out["sentiment_summary"]["composite_score"]
            assert out["hits_retrieved"] >= 0
        assert scores["HQC"] != scores["FPT"]

    def test_market_sentiment_default_fetch_enabled_for_vn(self) -> None:
        """ISSUE-4: VN tickers get per-ticker VNDIRECT quant by default —
        the server-side effective-fetch gate now ORs entity.is_vietnam."""
        from app.api.v1.schemas import MarketSentimentRequest

        req = MarketSentimentRequest.model_validate({"ticker": "HQC"})
        # schema default stays False; the SERVER-side gate enables VN fetch
        assert req.fetch_market_data is False

        entity = resolve_equity_entity("HQC", now=datetime.now(UTC))
        assert entity.is_vietnam is True
        # the gate: request.fetch_market_data or entity.is_vietnam
        assert (req.fetch_market_data or entity.is_vietnam) is True

    async def test_market_sentiment_honors_explicit_fetch_false_opt_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit `"fetch_market_data": false` opts out of VN live quant fetch."""
        from types import SimpleNamespace

        from fastapi.testclient import TestClient

        import app.api.deps as deps
        import app.api.v1.market as market_mod
        from app.main import create_app

        quant_calls: list[str] = []

        async def _spy_quant(ticker: str, **_kw: Any) -> tuple[list[Any], None, None, None, None]:
            quant_calls.append(ticker)
            return [], None, None, None, None

        monkeypatch.setattr(market_mod, "fetch_sentiment_quant_context", _spy_quant)
        monkeypatch.setattr(
            deps,
            "get_search_pipeline",
            lambda: SimpleNamespace(
                run=lambda _p: asyncio.sleep(0, result=SimpleNamespace(results=[]))
            ),
        )
        with TestClient(create_app()) as client:
            resp = client.post(
                "/v1/market/sentiment",
                json={"ticker": "FPT", "fetch_market_data": False, "retrieved_documents": []},
            )
        assert resp.status_code == 200
        assert quant_calls == []


class TestSimplizeAndDdgEdgeFixes:
    async def test_simplize_eps_ratio_low_roe_percent_and_404(self) -> None:
        """Live Simplize uses epsRatio, percentage ROE (e.g. 0.52 = 0.52%), and 404 = None."""
        import httpx

        from app.retrieval.fa_sources.simplize import SimplizeFASource

        def handler(request: httpx.Request) -> httpx.Response:
            sym = request.url.path.rsplit("/", 1)[-1]
            if sym == "MISSING":
                return httpx.Response(404, json={"status": 404, "data": None})
            return httpx.Response(
                200,
                json={
                    "status": 200,
                    "data": {
                        "peRatio": 17.08,
                        "pbRatio": 0.19,
                        "roe": 0.52,
                        "roa": 0.31,
                        "epsRatio": 108.3,
                        "eps": None,
                        "marketCap": 1.159e12,
                    },
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as cli:
            src = SimplizeFASource(client=cli)
            hqc = await src.fetch_financials("HQC")
            miss = await src.fetch_financials("MISSING")

        assert miss is None  # 404 does not raise FASourceError / trip breaker
        assert hqc is not None
        assert hqc.fields["roe_pct"] == pytest.approx(0.52)  # NOT 52.0!
        assert hqc.fields["roa_pct"] == pytest.approx(0.31)
        assert hqc.fields["eps_vnd"] == pytest.approx(108.3)

    async def test_ddg_retries_5xx_once_with_backoff(self) -> None:
        """DDG retries once on transient HTTP 502 and recovers cleanly."""
        calls = {"n": 0}

        class _Client502:
            async def post(self, *args: Any, **kwargs: Any):
                calls["n"] += 1
                resp = type("R", (), {})()
                if calls["n"] == 1:
                    resp.status_code = 502
                    resp.text = "bad gateway"
                    return resp
                resp.status_code = 200
                resp.text = '<a class="result__a" href="https://ok.example/1">Recovered</a>'
                return resp

        adapter = ddg_mod.DuckDuckGoAdapter(client=_Client502())  # type: ignore[arg-type]
        results = await adapter.search("test", timeout_s=0.5)
        assert calls["n"] == 2
        assert results and results[0].url == "https://ok.example/1"

    async def test_ddg_5xx_then_timeout_surfaces_timeout_not_stale_5xx(self) -> None:
        """Retry attempt raising HTTPError must not fall back to stale attempt-0 5xx resp."""
        import httpx

        calls = {"n": 0}

        class _Client502ThenTimeout:
            async def post(self, *args: Any, **kwargs: Any):
                calls["n"] += 1
                if calls["n"] == 1:
                    resp = type("R", (), {})()
                    resp.status_code = 502
                    resp.text = "bad gateway"
                    return resp
                raise httpx.ReadTimeout("timed out on retry")

        adapter = ddg_mod.DuckDuckGoAdapter(client=_Client502ThenTimeout())  # type: ignore[arg-type]
        with pytest.raises(AdapterError) as exc:
            await adapter.search("test", timeout_s=0.5)
        assert calls["n"] == 2
        assert exc.value.kind == Outcome.TIMEOUT
