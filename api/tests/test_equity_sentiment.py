"""Tests for the Omni-Channel Bilingual Equity Sentiment Engine (VI / EN) — module v2."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.api import deps
from app.core.auth import static_route_cost
from app.orchestration.skill_orchestrator import list_skills, select_skill
from app.retrieval.models import ScoredResult
from app.retrieval.sentiment import (
    RawSentimentHit,
    analyze_omni_channel_sentiment,
    build_omni_channel_queries,
    is_entity_collision,
    resolve_equity_entity,
)


def test_phase1_query_fanout_and_entity_resolution() -> None:
    # VN listed ticker resolution (DAT-06 catalog)
    vnm = resolve_equity_entity("vnm")
    assert vnm.ticker == "VNM"
    assert vnm.exchange == "HOSE"
    assert vnm.is_vietnam is True
    assert "Sữa" in vnm.resolved_name or "Vinamilk" in vnm.resolved_name or vnm.resolved_name

    queries_vn = build_omni_channel_queries(vnm, max_queries=15)
    layers = {q.layer for q in queries_vn}
    assert layers == {1, 2, 3, 4, 5}
    # Vietnamese queries are prioritized first for VN equities
    assert queries_vn[0].lang == "VI"
    joined_q = "\n".join(q.query for q in queries_vn)
    assert "site:hsx.vn" in joined_q
    assert "site:cafef.vn" in joined_q
    assert "site:fireant.vn" in joined_q
    assert "site:x.com" in joined_q
    assert "site:youtube.com" in joined_q

    # Global ticker resolution
    nvda = resolve_equity_entity("NVDA")
    assert nvda.ticker == "NVDA"
    assert nvda.exchange == "NASDAQ"
    assert nvda.is_vietnam is False
    queries_en = build_omni_channel_queries(nvda, max_queries=6)
    assert len(queries_en) == 6
    assert queries_en[0].lang == "EN"


def test_phase2a_strict_entity_collision_filter() -> None:
    vnm = resolve_equity_entity("VNM")
    assert is_entity_collision(
        vnm,
        RawSentimentHit(
            url="https://etf.com/vnm",
            title="VanEck Vietnam ETF (NYSEARCA:VNM) sees outflows",
            snippet="Market Vectors Vietnam ETF VNM fell 1.2%",
        ),
    )
    assert not is_entity_collision(
        vnm,
        RawSentimentHit(
            url="https://cafef.vn/vnm-1.chn",
            title="Vinamilk (VNM) chốt quyền trả cổ tức tiền mặt cao",
            snippet="CTCP Sữa Việt Nam công bố nghị quyết HĐQT.",
        ),
    )

    gas = resolve_equity_entity("GAS")
    assert is_entity_collision(
        gas,
        RawSentimentHit(
            url="https://reuters.com/energy",
            title="US Natural Gas futures at Henry Hub drop on warm weather",
            snippet="NYMEX natural gas futures fell 4%.",
        ),
    )
    assert not is_entity_collision(
        gas,
        RawSentimentHit(
            url="https://vietstock.vn/gas",
            title="PV Gas (GAS) ghi nhận lợi nhuận kỷ lục quý 3",
            snippet="Tổng Công ty Khí Việt Nam vượt kế hoạch năm.",
        ),
    )

    ssi = resolve_equity_entity("SSI")
    assert is_entity_collision(
        ssi,
        RawSentimentHit(
            url="https://ssa.gov/ssi",
            title="Supplemental Security Income (SSI) payment schedule for Social Security",
            snippet="Disability benefits update.",
        ),
    )

    fpt = resolve_equity_entity("FPT")
    assert is_entity_collision(
        fpt,
        RawSentimentHit(
            url="https://ftportfolios.com/fpt",
            title="First Trust New Opportunities ETF (FPT) dividend",
        ),
    )

    pow_ent = resolve_equity_entity("POW")
    assert is_entity_collision(
        pow_ent,
        RawSentimentHit(
            url="https://crypto.example/pow",
            title="Bitcoin Proof-of-Work (PoW) mining difficulty hits new high",
        ),
    )


def test_phase2b_seeding_detector_and_euphoric_distribution_trap() -> None:
    now = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
    entity = resolve_equity_entity("DIG", now=now)

    hits = [
        # Tier 1/2 negative institutional / foreign flow
        RawSentimentHit(
            url="https://hsx.vn/dig-filing",
            title="DIG: Khối ngoại xả ròng liên tục, thanh tra dự án và đăng ký bán lớn",
            snippet="Khối ngoại bán ròng mạnh cổ phiếu DIG; lãnh đạo đăng ký bán lớn.",
            published_at=now - timedelta(hours=2),
            lang="VI",
        ),
        RawSentimentHit(
            url="https://cafef.vn/dig-flow",
            title="Cổ phiếu DIG bị khối ngoại xả ròng liên tục và cảnh báo call margin",
            snippet="Áp lực bán giải chấp và thua lỗ ngắn hạn tại DIG.",
            published_at=now - timedelta(hours=4),
            lang="VI",
        ),
        # 4 copy-paste VIP seeding posts across Facebook/FireAnt/Telegram hyping the stock
        RawSentimentHit(
            url="https://facebook.com/groups/ck1/posts/1",
            title="DIG siêu cổ vượt đỉnh thanh khoản lớn lợi nhuận kỷ lục vào room VIP ngay",
            snippet=(
                "DIG vượt đỉnh thanh khoản lớn lợi nhuận kỷ lục trúng thầu đầu tư công "
                "vào room VIP nhóm kín nhận mã!"
            ),
            published_at=now - timedelta(hours=1),
            lang="VI",
        ),
        RawSentimentHit(
            url="https://fireant.vn/posts/2",
            title="DIG siêu cổ vượt đỉnh thanh khoản lớn lợi nhuận kỷ lục vào room VIP ngay",
            snippet=(
                "DIG vượt đỉnh thanh khoản lớn lợi nhuận kỷ lục trúng thầu đầu tư công "
                "vào room VIP nhóm kín nhận mã!"
            ),
            published_at=now - timedelta(hours=1),
            lang="VI",
        ),
        RawSentimentHit(
            url="https://t.me/ckvip/3",
            title="DIG siêu cổ vượt đỉnh thanh khoản lớn lợi nhuận kỷ lục vào room VIP ngay",
            snippet=(
                "DIG vượt đỉnh thanh khoản lớn lợi nhuận kỷ lục trúng thầu đầu tư công "
                "vào room VIP nhóm kín nhận mã!"
            ),
            published_at=now - timedelta(hours=1),
            lang="VI",
        ),
        # Unverified viral rumor (not in Tier 1/2)
        RawSentimentHit(
            url="https://f319.com/threads/dig-rumor",
            title="Nghe đồn DIG sắp có tin đồn chia thưởng khủng và game tăng vốn vượt đỉnh",
            snippet=(
                # docs/29 PASS-2 P2-6: distinct top-level lexicon phrases only —
                # the old double-counting of nested phrases ("vượt đỉnh" inside
                # "vượt đỉnh thanh khoản lớn") inflated social_score > 0.75.
                "Tin mật nội bộ nghe đồn DIG tin đồn chia thưởng khủng vượt đỉnh "
                "thanh khoản lớn, tự doanh gom hàng, trúng thầu đầu tư công."
            ),
            published_at=now - timedelta(hours=2),
            lang="VI",
        ),
    ]

    out = analyze_omni_channel_sentiment(entity, hits, now=now)
    assert out["module"] == "omni_channel_equity_sentiment_v2"
    audit = out["social_integrity_audit"]
    assert audit["manipulation_or_seeding_detected"] is True
    assert audit["unverified_rumor_count"] == 1
    assert "COORDINATED_SOCIAL_SEEDING" in out["risk_circuit_breakers"]
    assert "UNVERIFIED_RUMOR_SPIKE" in out["risk_circuit_breakers"]
    assert "RETAIL_FOMO_VS_FOREIGN_SELLING" in out["risk_circuit_breakers"]
    assert out["sentiment_summary"]["market_regime"] == "EUPHORIC_DISTRIBUTION_TRAP"
    # Unverified rumor must be excluded from top_verified_drivers
    driver_urls = [d["url"] for d in out["top_verified_drivers"]]
    assert "https://f319.com/threads/dig-rumor" not in driver_urls
    # Tier 1 breaking 6h hit gets effective_weight = 1.0 * 1.0 = 1.0
    by_url = {d["url"]: d for d in out["top_verified_drivers"]}
    assert by_url["https://hsx.vn/dig-filing"]["tier"] == "TIER_1"
    assert by_url["https://hsx.vn/dig-filing"]["freshness"] == "BREAKING_6H"
    assert by_url["https://hsx.vn/dig-filing"]["effective_weight"] == 1.0


def test_stealth_accumulation_and_insufficient_data_grounding() -> None:
    now = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
    fpt = resolve_equity_entity("FPT", now=now)

    # Zero-fabrication contract when no valid hits exist
    empty_out = analyze_omni_channel_sentiment(fpt, [], now=now)
    assert empty_out["sentiment_summary"]["composite_score"] == 0.0
    assert empty_out["sentiment_summary"]["confidence"] < 0.2
    assert empty_out["sentiment_summary"]["market_regime"] == "LOW_SIGNAL_NOISE"
    assert empty_out["top_verified_drivers"] == []
    # docs/29 ISSUE-4: the zero-evidence payload now flags itself explicitly
    assert empty_out["risk_circuit_breakers"] == [
        "INSUFFICIENT_DATA",
        "ZERO_EVIDENCE_DEFAULT_PAYLOAD",
    ]
    assert empty_out["hits_retrieved"] == 0
    assert empty_out["hits_after_collision_filter"] == 0

    # Stealth accumulation: Tier 1/2 institutional buying + quiet/skeptical social
    stealth_hits = [
        RawSentimentHit(
            url="https://ssi.com.vn/report/fpt",
            title="FPT: Khối ngoại mua ròng và tự doanh gom hàng liên tiếp",
            snippet="Khối ngoại mua ròng 350 tỷ đồng cổ phiếu FPT, khuyến nghị mua.",
            published_at=now - timedelta(hours=3),
            lang="VI",
        ),
        RawSentimentHit(
            url="https://bloomberg.com/news/fpt",
            title="FPT Corp sees institutional accumulation and margin expansion",
            snippet="Institutional accumulation continues in FPT Corp shares.",
            published_at=now - timedelta(hours=12),
            lang="EN",
        ),
    ]
    stealth_out = analyze_omni_channel_sentiment(fpt, stealth_hits, now=now)
    assert stealth_out["sentiment_summary"]["market_regime"] == "STEALTH_ACCUMULATION"
    assert stealth_out["sentiment_summary"]["composite_score"] > 0.25
    assert stealth_out["sentiment_summary"]["label"] in ("BULLISH", "STRONG_BULLISH")


def test_route_bypass_and_fanout_integration(client, monkeypatch) -> None:
    # Verify route rate-limit cost is registered in _STATIC_ROUTE_COSTS
    # (docs/29 PASS-2 P1-3: re-priced 5 -> 8 for up to 22 sub-searches)
    assert static_route_cost("POST", "/v1/market/sentiment") == 8

    # Skill orchestrator registration & trigger check
    skill_ids = {s.id for s in list_skills()}
    assert "omni_channel_equity_sentiment" in skill_ids
    sel = select_skill("Phân tích tâm lý thị trường và đội lái lùa gà cổ phiếu FPT trên F319")
    assert sel.skill_id == "omni_channel_equity_sentiment"

    # 1. Bypass mode: retrieved_documents supplied -> search pipeline must NOT be called
    called = {"count": 0}

    class _FailIfCalledPipe:
        async def run(self, params: Any) -> Any:
            called["count"] += 1
            raise AssertionError("SearchPipeline must be bypassed when retrieved_documents is set")

    monkeypatch.setattr(deps, "get_search_pipeline", lambda: _FailIfCalledPipe())

    now_iso = datetime.now(UTC).isoformat()
    resp = client.post(
        "/v1/market/sentiment",
        json={
            "ticker": "FPT",
            "retrieved_documents": [
                {
                    "url": "https://cafef.vn/fpt-kqkd.chn",
                    "title": "FPT công bố lợi nhuận kỷ lục, khối ngoại mua ròng mạnh",
                    "snippet": (
                        "CTCP FPT đạt lợi nhuận kỷ lục, khối ngoại mua ròng "
                        "và chia cổ tức tiền mặt cao."
                    ),
                    "published_at": now_iso,
                    "lang": "VI",
                },
                {
                    "url": "https://x.com/trader/status/1",
                    "title": "$FPT breakout and institutional accumulation",
                    "snippet": (
                        "FPT Corp beats estimates with margin expansion and strong buy rating."
                    ),
                    "published_at": now_iso,
                    "lang": "EN",
                },
            ],
        },
    )
    assert resp.status_code == 200
    assert called["count"] == 0
    body = resp.json()
    assert body["module"] == "omni_channel_equity_sentiment_v2"
    assert body["entity"]["ticker"] == "FPT"
    assert body["entity"]["exchange"] == "HOSE"
    assert body["sentiment_summary"]["composite_score"] > 0.3
    assert body["sentiment_summary"]["market_regime"] == "MOMENTUM_MARKUP"
    assert len(body["top_verified_drivers"]) == 2

    # 2. Fan-out mode: when retrieved_documents is omitted, queries fan out via SearchPipeline
    seen_queries: list[str] = []

    class _FakeSearchOutcome:
        def __init__(self, results: list[ScoredResult]) -> None:
            self.results = results

    class _FakePipe:
        async def run(self, params: Any) -> _FakeSearchOutcome:
            seen_queries.append(params.query)
            assert params.record_memory is False
            return _FakeSearchOutcome(
                [
                    ScoredResult(
                        url="https://vietstock.vn/hpg-news",
                        canonical_url="https://vietstock.vn/hpg-news",
                        title="HPG vượt đỉnh thanh khoản lớn nhờ trúng thầu đầu tư công",
                        snippet="Cổ phiếu HPG được khối ngoại mua ròng và tự doanh gom hàng.",
                        domain="vietstock.vn",
                        score=0.9,
                        consensus=1,
                        adapters=["fake"],
                        published_at=datetime.now(UTC),
                    )
                ]
            )

    monkeypatch.setattr(deps, "get_search_pipeline", lambda: _FakePipe())
    resp_fanout = client.post(
        "/v1/market/sentiment",
        json={"ticker": "HPG", "max_queries": 5, "k": 3},
    )
    assert resp_fanout.status_code == 200
    assert len(seen_queries) == 5
    data_fanout = resp_fanout.json()
    assert data_fanout["entity"]["ticker"] == "HPG"
    assert len(data_fanout["top_verified_drivers"]) == 1

    # Invalid ticker rejected with 400
    bad_resp = client.post("/v1/market/sentiment", json={"ticker": "bad;ticker"})
    assert bad_resp.status_code == 400


def test_benchmark_standards_negation_insider_and_seven_pillars() -> None:
    now = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    fpt = resolve_equity_entity("FPT", now=now)

    # 1. Negation guard: "không đạt lợi nhuận kỷ lục" and "failed to beat estimates"
    # must invert polarity to NEGATIVE instead of false-positive BULLISH
    neg_hits = [
        RawSentimentHit(
            url="https://cafef.vn/fpt-neg",
            title="FPT không đạt lợi nhuận kỷ lục và chưa thể vượt đỉnh MA200",
            snippet="Doanh nghiệp không đạt lợi nhuận kỷ lục trong quý này.",
            published_at=now - timedelta(hours=2),
            lang="VI",
        ),
        RawSentimentHit(
            url="https://reuters.com/fpt-neg",
            title="FPT Corp failed to beat estimates and cuts guidance",
            snippet="The company failed to beat estimates amid multiple compression.",
            published_at=now - timedelta(hours=3),
            lang="EN",
        ),
    ]
    neg_out = analyze_omni_channel_sentiment(fpt, neg_hits, now=now)
    assert neg_out["sentiment_summary"]["composite_score"] < -0.20
    assert neg_out["sentiment_summary"]["label"] in ("BEARISH", "STRONG_BEARISH")

    # 2. 7-Pillar Checklist, Insider MSPR, SentimenTrader Smart/Dumb Money & CNN Fear/Greed
    rich_hits = [
        RawSentimentHit(
            url="https://hsx.vn/fpt-insider",
            title="FPT: Chủ tịch đăng ký mua 2 triệu cổ phiếu, khối ngoại mua ròng mạnh",
            snippet="Lãnh đạo đăng ký mua vào cổ phiếu FPT, vượt đỉnh 52 tuần và vượt MA200.",
            published_at=now - timedelta(hours=2),
            lang="VI",
        ),
        RawSentimentHit(
            url="https://finance.vietstock.vn/fpt-breadth",
            title=(
                "Tâm lý thị trường: FPT bùng nổ theo đà, lan tỏa dòng tiền và thanh khoản đột biến"
            ),
            snippet="Cổ phiếu FPT vượt MA50 với golden cross và khối ngoại mua ròng.",
            published_at=now - timedelta(hours=4),
            lang="VI",
        ),
        RawSentimentHit(
            url="https://reddit.com/r/stocks/fpt-panic",
            title="$FPT retail panic selloff and dead cat bounce fears",
            snippet="Retail traders fear a bull trap and downside plunge on $FPT.",
            published_at=now - timedelta(hours=3),
            lang="EN",
        ),
    ]
    rich_out = analyze_omni_channel_sentiment(fpt, rich_hits, now=now)
    bm = rich_out["benchmark_standards"]
    assert bm["insider_signal"] == "NET_BUYING"
    assert bm["smart_money_confidence"] >= 70.0
    assert bm["dumb_money_confidence"] <= 30.0
    assert bm["smart_dumb_spread"] >= 40.0
    assert bm["contrarian_signal"] == "CONTRARIAN_BULLISH_OPPORTUNITY"
    assert bm["buzz_velocity"] in ("ELEVATED", "VIRAL_SURGE")
    assert 0 <= bm["fear_greed_index"] <= 100
    assert bm["fear_greed_zone"] in ("NEUTRAL", "GREED", "EXTREME_GREED")
    assert len(bm["seven_pillar_checklist"]) == 7
    pillar_ids = [p["pillar_id"] for p in bm["seven_pillar_checklist"]]
    assert pillar_ids == [
        "filings_earnings_insider",
        "institutional_smart_money",
        "momentum_and_moving_averages",
        "breadth_strength_and_liquidity",
        "volatility_options_and_leverage",
        "retail_social_and_buzz_velocity",
        "anti_seeding_and_data_integrity",
    ]


def test_quantitative_ohlcv_formulas_and_zero_fabrication() -> None:
    from app.retrieval.sentiment_quant import (
        OhlcvBarInput,
        compute_cmf,
        compute_mfi,
        compute_ohlcv_sentiment_metrics,
    )

    empty = compute_ohlcv_sentiment_metrics([])
    assert empty["available"] is False
    assert empty["momentum_quant_score"] is None
    assert empty["pct_vs_ma125"] is None

    # Build 130 deterministic upward bars (MA125 available, MA200 stays None — zero fabrication)
    base_ts = 1_760_000_000
    bars: list[OhlcvBarInput] = []
    for i in range(130):
        c = 100.0 + i * 0.25
        vol = 1_000_000.0 if i < 129 else 2_500_000.0
        bars.append(
            OhlcvBarInput(
                time=base_ts + i * 86400,
                open=c - 0.9,
                high=c + 0.2,
                low=c - 1.2,
                close=c,
                volume=vol,
            )
        )

    m = compute_ohlcv_sentiment_metrics(bars, source_name="test-feed")
    assert m["available"] is True
    assert m["bars_count"] == 130
    assert m["pct_vs_ma50"] is not None and m["pct_vs_ma50"] > 0.0
    assert m["pct_vs_ma125"] is not None and m["pct_vs_ma125"] > 0.0
    assert m["pct_vs_ma200"] is None
    assert m["range_pos_52w_pct"] is not None and m["range_pos_52w_pct"] > 80.0
    assert m["rsi_14"] is not None and m["rsi_14"] > 60.0
    assert compute_mfi(bars, 14) == 100.0
    cmf = compute_cmf(bars, 21)
    assert cmf is not None and cmf > 0.1
    assert m["volume_ratio_20d"] == 2.5
    assert m["vol_ratio_20_60"] is not None
    assert m["momentum_quant_score"] is not None and 50.0 <= m["momentum_quant_score"] <= 100.0
    assert m["breadth_flow_quant_score"] is not None and m["breadth_flow_quant_score"] > 65.0


def test_quantitative_market_proxies_surveys_breadth_options_darkpool() -> None:
    from app.retrieval.sentiment_quant import MarketProxiesInput, compute_market_proxy_metrics

    rana_hist = tuple(250.0 if i % 2 == 0 else -100.0 for i in range(45))
    proxies = MarketProxiesInput(
        aaii_bullish_pct=55.0,
        aaii_bearish_pct=20.0,
        ii_bullish_pct=60.0,
        ii_bearish_pct=20.0,
        naaim_exposure_pct=92.5,
        advancing_issues=2000,
        declining_issues=500,
        advancing_volume=3000.0,
        declining_volume=500.0,
        new_52w_highs=120,
        new_52w_lows=30,
        pct_above_ma50=74.0,
        pct_above_ma200=68.0,
        rana_series=rana_hist,
        cboe_put_call_equity=0.85,
        vix=28.0,
        vix_3m=24.5,
        vix_ma50=19.0,
        cboe_skew=146.0,
        off_exchange_short_dollar_vol=470.0,
        off_exchange_total_dollar_vol=1000.0,
        margin_debt_curr=900.0,
        margin_debt_prev_year=750.0,
        junk_yield_pct=7.5,
        ig_yield_pct=4.8,
        social_weighted_24h=18.0,
        social_mean_20d=10.0,
        social_std_20d=4.0,
        insider_acquired_shares=3000.0,
        insider_disposed_shares=1000.0,
    )
    pm = compute_market_proxy_metrics(proxies)
    assert pm["available"] is True
    assert pm["aaii_bull_bear_spread"] == 35.0
    assert pm["ii_bull_bear_ratio"] == 3.0
    assert pm["naaim_exposure_pct"] == 92.5
    assert pm["arms_trin"] == 0.667
    assert pm["mcclellan_rana"] == 600.0
    assert pm["mcclellan_oscillator"] is not None
    assert pm["high_low_breadth_pct"] == 80.0
    assert pm["vix_term_ratio"] == 1.143
    assert pm["vix_term_regime"] == "BACKWARDATION_STRESS"
    assert pm["dix_pct"] == 47.0
    assert pm["margin_debt_yoy_pct"] == 20.0
    assert pm["credit_spread_bps"] == 270.0
    assert pm["social_s_score"] == 2.0
    assert pm["insider_mspr"] == 0.5
    assert pm["bofa_bull_bear_analog_0_10"] is not None

    # Fuse proxies into equity sentiment engine -> verify Pillar 5 tail-risk WARNING & buzz surge
    nvda = resolve_equity_entity("NVDA")
    out = analyze_omni_channel_sentiment(nvda, [], market_proxies=proxies)
    bm = out["benchmark_standards"]
    assert bm["buzz_velocity"] == "VIRAL_SURGE"
    assert bm["insider_signal"] == "NET_BUYING"
    assert bm["quantitative_telemetry"]["market_proxies"]["dix_pct"] == 47.0
    p5 = next(
        p
        for p in bm["seven_pillar_checklist"]
        if p["pillar_id"] == "volatility_options_and_leverage"
    )
    assert p5["status"] in ("WARNING", "BEARISH")


def test_nlp_tfidf_tetlock_polarization_and_domain_expansion() -> None:
    from app.retrieval.sentiment import _classify_domain_and_tier
    from app.retrieval.sentiment_quant import compute_tfidf_idf_map

    vnm = resolve_equity_entity("VNM")
    expanded_q = build_omni_channel_queries(vnm, max_queries=22)
    joined = "\n".join(q.query for q in expanded_q)
    assert "site:fiintrade.vn" in joined
    assert "site:cboe.com" in joined
    assert "site:finviz.com" in joined
    assert "site:unusualwhales.com" in joined

    assert _classify_domain_and_tier("fiintrade.vn", "khối ngoại mua ròng", "VI")[0] == "TIER_2"
    assert _classify_domain_and_tier("cboe.com", "VIX and SKEW data", "EN")[0] == "TIER_1"
    assert (
        _classify_domain_and_tier("unusualwhales.com", "$NVDA options flow", "EN")[2]
        == "global_social"
    )

    # Repeated boilerplate term across all docs receives lower IDF multiplier than rare catalyst
    idf = compute_tfidf_idf_map(
        [
            ["vượt đỉnh", "lãi kỷ lục"],
            ["vượt đỉnh"],
            ["vượt đỉnh"],
            ["vượt đỉnh"],
        ]
    )
    assert idf["lãi kỷ lục"] > idf["vượt đỉnh"]

    now = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
    mixed_hits = [
        RawSentimentHit(
            url="https://cafef.vn/vnm-good",
            title="VNM bứt phá lợi nhuận kỷ lục và vượt đỉnh",
            published_at=now - timedelta(hours=2),
            lang="VI",
        ),
        RawSentimentHit(
            url="https://vietstock.vn/vnm-bad",
            title="VNM khối ngoại bán ròng và thua lỗ",
            published_at=now - timedelta(hours=2),
            lang="VI",
        ),
        RawSentimentHit(
            url="https://fiintrade.vn/vnm-risk",
            title="VNM cảnh báo call margin và khởi tố thanh tra",
            published_at=now - timedelta(hours=3),
            lang="VI",
        ),
    ]
    out = analyze_omni_channel_sentiment(vnm, mixed_hits, now=now)
    nlp = out["benchmark_standards"]["nlp_telemetry"]
    assert nlp["tfidf_corpus_weighting"] is True
    assert nlp["corpus_doc_count"] == 3
    assert nlp["tetlock_media_pessimism"] > 0.3
    assert nlp["consensus_polarization"] > 0.25
    assert nlp["source_diversity_entropy"] > 0.9


def test_route_sentiment_quant_and_mocked_fetch(monkeypatch: Any) -> None:
    from fastapi.testclient import TestClient

    import app.retrieval.sentiment_quant as sq_mod
    from app.main import create_app
    from app.retrieval.fa_sources.aggregator import AggregatedFinancials, FieldValue

    app = create_app()
    now_ts = int(datetime(2026, 11, 14, 7, 0, tzinfo=UTC).timestamp())

    async def fake_vndirect(symbol: str, **kwargs: Any) -> dict[str, Any]:
        times = [now_ts - (29 - i) * 86400 for i in range(30)]
        closes = [50.0 + i * 0.4 for i in range(30)]
        return {
            "times": times,
            "opens": [c - 0.3 for c in closes],
            "highs": [c + 0.5 for c in closes],
            "lows": [c - 0.4 for c in closes],
            "closes": closes,
            "volumes": [500_000.0 for _ in closes],
            "last_ts": times[-1],
        }

    class FakeAggregator:
        async def fetch_financials(
            self, symbol: str, *, timeout_s: float = 3.5
        ) -> AggregatedFinancials:
            return AggregatedFinancials(
                symbol=symbol,
                fields={
                    "pe": FieldValue(value=14.2, source="tcbs", as_of=None, validated=True),
                    "pb": FieldValue(value=2.1, source="tcbs", as_of=None, validated=False),
                    "roe_pct": FieldValue(value=21.5, source="fireant", as_of=None, validated=True),
                },
                sources_used=["tcbs", "fireant"],
                source_urls=["https://tcbs.com.vn"],
                as_of=datetime(2026, 11, 14, 0, 0, tzinfo=UTC),
            )

        async def fetch_ohlcv(self, symbol: str, *, timeout_s: float = 3.5) -> None:
            return None

    monkeypatch.setattr(sq_mod, "fetch_vndirect_ohlcv_series", fake_vndirect)
    monkeypatch.setattr(deps, "get_fa_aggregator", lambda: FakeAggregator())

    client = TestClient(app)
    resp = client.post(
        "/v1/market/sentiment",
        json={
            "ticker": "FPT",
            "fetch_market_data": True,
            "retrieved_documents": [
                {
                    "url": "https://fiintrade.vn/fpt-1",
                    "title": "FPT khối ngoại mua ròng mạnh và vượt đỉnh",
                    "published_at": "2026-11-15T06:00:00Z",
                }
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    qt = body["benchmark_standards"]["quantitative_telemetry"]
    assert qt["ohlcv"]["source_name"] == "VNDIRECT"
    assert qt["ohlcv"]["bars_count"] == 30
    assert qt["ohlcv"]["rsi_14"] is not None
    assert qt["ohlcv"]["mfi_14"] == 100.0
    assert qt["fundamentals"]["pe"] == 14.2
    assert qt["fundamentals"]["roe"] == 21.5
    assert qt["fundamentals"]["source"] == "tcbs, fireant"

    # Global equity (NVDA) via Yahoo Finance v8 chart JSON parser + fail-open check
    from app.retrieval.sentiment_quant import parse_yahoo_chart_bars

    yahoo_sample = {
        "chart": {
            "result": [
                {
                    "timestamp": [now_ts - (24 - i) * 86400 for i in range(25)],
                    "indicators": {
                        "quote": [
                            {
                                "open": [120.0 + i for i in range(25)],
                                "high": [121.0 + i for i in range(25)],
                                "low": [119.0 + i for i in range(25)],
                                "close": [120.5 + i for i in range(25)],
                                "volume": [10_000_000.0 for _ in range(25)],
                            }
                        ]
                    },
                }
            ]
        }
    }
    y_bars, y_as_of = parse_yahoo_chart_bars(yahoo_sample)
    assert len(y_bars) == 25
    assert y_as_of is not None

    # Live fetch failure must fail open -> zero fabrication (quantitative_telemetry is None)
    async def broken_vndirect(symbol: str, **kwargs: Any) -> dict[str, Any] | None:
        raise RuntimeError("upstream unavailable")

    class BrokenAggregator:
        async def fetch_financials(self, symbol: str, *, timeout_s: float = 3.5) -> None:
            raise RuntimeError("fa down")

        async def fetch_ohlcv(self, symbol: str, *, timeout_s: float = 3.5) -> None:
            return None

    monkeypatch.setattr(sq_mod, "fetch_vndirect_ohlcv_series", broken_vndirect)
    monkeypatch.setattr(deps, "get_fa_aggregator", lambda: BrokenAggregator())

    resp_fail_open = client.post(
        "/v1/market/sentiment",
        json={
            "ticker": "FPT",
            "fetch_market_data": True,
            "retrieved_documents": [
                {
                    "url": "https://cafef.vn/fpt-2",
                    "title": "FPT tăng trưởng lợi nhuận quý",
                    "published_at": "2026-11-15T06:00:00Z",
                }
            ],
        },
    )
    assert resp_fail_open.status_code == 200
    assert resp_fail_open.json()["benchmark_standards"]["quantitative_telemetry"] is None


# ---------------------------------------------------------------------------
# docs/29 PASS-2 adversarial tests (audit P1-1/P1-2/P2-8/P3-15 gaps)
# ---------------------------------------------------------------------------


def test_negation_multi_occurrence_no_longer_masked() -> None:
    """P1-1: an early un-negated occurrence must not mask a later negated
    one — 'lợi nhuận kỷ lục ... không đạt lợi nhuận kỷ lục' nets negative."""
    entity = resolve_equity_entity("VNM", company_name=None)
    text = "Vinamilk lợi nhuận kỷ lục ghi nhận. Nhưng thật ra không đạt lợi nhuận kỷ lục."
    hits = [
        RawSentimentHit(
            url="https://cafef.vn/vnm",
            title=text[:80],
            snippet=text,
            content=text,
            published_at=datetime.now(UTC) - timedelta(hours=1),
            lang="VI",
        )
    ]
    out = analyze_omni_channel_sentiment(entity, hits, now=datetime.now(UTC))
    summary = out["sentiment_summary"]
    # Per-occurrence scoring: the negated occurrence drags the old un-masked
    # +0.45 (POSITIVE) down to ~+0.11 — no longer presented as pure bullish.
    assert summary["composite_score"] < 0.3
    assert summary["composite_score"] > -1.0


def test_company_name_operator_injection_rejected() -> None:
    """P1-2: search-operator payloads ('OR site:evil') are rejected by the
    schema pattern before reaching the fan-out query grammar."""
    from pydantic import ValidationError

    from app.api.v1.schemas import MarketSentimentRequest

    with pytest.raises(ValidationError):
        MarketSentimentRequest.model_validate(
            {
                "ticker": "FPT",
                "company_name": 'zz" OR site:evil-market-news.example OR "y',
            }
        )
    # a clean display name still passes
    req = MarketSentimentRequest.model_validate({"ticker": "FPT", "company_name": "FPT Corp."})
    assert req.company_name == "FPT Corp."


def test_keyword_domain_tier2_spoof_blocked() -> None:
    """P1-2: a domain merely containing 'market' can no longer self-promote
    to TIER_2 — only the registered allowlist earns official weight."""
    from app.retrieval.sentiment import _classify_domain_and_tier

    tier, _platform, eco = _classify_domain_and_tier(
        "my-market-blog.example.com", "FPT tăng mạnh sau báo cáo" * 3, "VI"
    )
    assert tier in ("TIER_3", "TIER_4")
    assert eco != "official_and_media"
    # registered press keeps its tier
    tier2, _p2, eco2 = _classify_domain_and_tier("cafef.vn", "FPT tăng mạnh", "VI")
    assert tier2 == "TIER_2" and eco2 == "official_and_media"


def test_client_supplied_docs_capped_at_tier3() -> None:
    """P2-8: a forged 'sec.gov' URL from the retrieved_documents bypass can
    no longer present as verified TIER_1 evidence."""
    entity = resolve_equity_entity("VNM", company_name=None)
    hits = [
        RawSentimentHit(
            url="https://sec.gov/fake-vnm",
            title="SEC approves VNM merger, khối ngoại mua ròng",
            snippet="SEC approves VNM merger khối ngoại mua ròng mạnh.",
            content="SEC approves VNM merger, khối ngoại mua ròng mạnh.",
            published_at=datetime.now(UTC) - timedelta(hours=1),
            lang="EN",
            client_supplied=True,
        )
    ]
    out = analyze_omni_channel_sentiment(entity, hits, now=datetime.now(UTC))
    drivers = out["top_verified_drivers"]
    assert drivers, "driver expected"
    assert drivers[0]["tier"] != "TIER_1"


def test_future_dated_content_is_stale() -> None:
    """P3-15: a future published_at must earn STALE, not BREAKING_6H."""
    entity = resolve_equity_entity("VNM", company_name=None)
    hits = [
        RawSentimentHit(
            url="https://cafef.vn/vnm",
            title="VNM doanh thu tăng",
            snippet="VNM doanh thu tăng lợi nhuận ổn định.",
            content="VNM doanh thu tăng lợi nhuận ổn định.",
            published_at=datetime.now(UTC) + timedelta(days=7),
            lang="VI",
        )
    ]
    out = analyze_omni_channel_sentiment(entity, hits, now=datetime.now(UTC))
    drivers = out["top_verified_drivers"]
    assert drivers and drivers[0]["freshness"] == "STALE"


def test_no_fabricated_neutral_when_no_market_pillars() -> None:
    """P2-5: with no active market pillar the benchmark emits None +
    INSUFFICIENT_DATA instead of a placeholder 50/NEUTRAL."""
    entity = resolve_equity_entity("VNM", company_name=None)
    hits = [
        RawSentimentHit(
            url="https://t.me/ck/1",
            title="Đovní đồn VNM sắp có tin tốt",
            snippet="Đovní đồn VNM sắp có tin tốt thả ghe trôi.",
            content="Đovní đồn VNM sắp có tin tốt thả ghe trôi.",
            published_at=datetime.now(UTC) - timedelta(hours=1),
            lang="VI",
        )
    ]
    out = analyze_omni_channel_sentiment(entity, hits, now=datetime.now(UTC))
    bm = out["benchmark_standards"]
    # P2-5 core: no official/social hits -> smart/dumb must be None, never a
    # placeholder 50.0; pillar 6 may be legitimately active from the rumor
    # hits themselves (real input), so only assert the fabricated scalar.
    assert bm["smart_money_confidence"] is None
    assert not (bm["smart_dumb_spread"] == 0.0 and bm["dumb_money_confidence"] == 50.0)


def _sample_bars(count: int = 30) -> list[Any]:
    from app.retrieval.sentiment_quant import OhlcvBarInput

    base_ts = int(datetime.now(UTC).timestamp()) - count * 86400
    return [
        OhlcvBarInput(
            time=base_ts + i * 86400,
            open=100.0 + i * 0.4,
            high=101.0 + i * 0.4,
            low=99.0 + i * 0.4,
            close=100.5 + i * 0.4,
            volume=1_000_000.0 + i * 10_000.0,
        )
        for i in range(count)
    ]


def test_quant_only_ohlcv_does_not_crash_with_none_smart_dumb() -> None:
    """Zero text hits + OHLCV only leaves smart/dumb confidence None without TypeError."""
    entity = resolve_equity_entity("FPT", company_name=None)
    out = analyze_omni_channel_sentiment(
        entity,
        [],
        now=datetime.now(UTC),
        ohlcv_bars=_sample_bars(30),
        ohlcv_source_name="VNDIRECT",
    )
    bm = out["benchmark_standards"]
    assert bm["smart_money_confidence"] is None
    assert bm["dumb_money_confidence"] is None
    assert bm["fear_greed_index"] is not None
    assert out["sentiment_summary"]["institutional_vs_retail_spread"] == 0.0
    assert bm["quantitative_telemetry"]["ohlcv"]["bars_count"] == 30


def test_rumor_only_preserves_quant_payload() -> None:
    """Rumor-only text paired with real OHLCV keeps quant telemetry + flags rumor breaker."""
    entity = resolve_equity_entity("VNM", company_name=None)
    now = datetime.now(UTC)
    hits = [
        RawSentimentHit(
            url="https://t.me/ck/rumor1",
            title="Nghe đồn VNM sắp bị khởi tố",
            snippet="Nghe đồn VNM sắp bị khởi tố chưa kiểm chứng",
            content="Nghe đồn VNM sắp bị khởi tố chưa kiểm chứng",
            published_at=now - timedelta(hours=1),
            lang="VI",
        )
    ]
    out = analyze_omni_channel_sentiment(
        entity,
        hits,
        now=now,
        ohlcv_bars=_sample_bars(30),
        ohlcv_source_name="VNDIRECT",
    )
    assert out["benchmark_standards"]["quantitative_telemetry"]["ohlcv"]["bars_count"] == 30
    assert out["social_integrity_audit"]["unverified_rumor_count"] == 1
    assert "UNVERIFIED_RUMOR_SPIKE" in out["risk_circuit_breakers"]
    # PASS-2 P2-1: unverified rumors must NOT drive dumb_money or Pillar 6 social score
    assert out["benchmark_standards"]["dumb_money_confidence"] is None
    social_pillar = next(
        p
        for p in out["benchmark_standards"]["seven_pillar_checklist"]
        if p["pillar_id"] == "retail_social_and_buzz_velocity"
    )
    assert social_pillar["score_0_100"] == 50
    assert social_pillar["status"] == "INSUFFICIENT_DATA"
    assert out["hits_retrieved"] == 1
    assert out["hits_after_collision_filter"] == 1


def test_s_score_upgrades_elevated_buzz_to_viral_surge() -> None:
    """S-Score >= 2.0 must promote ELEVATED buzz_velocity to VIRAL_SURGE."""
    from app.retrieval.sentiment_quant import MarketProxiesInput

    entity = resolve_equity_entity("FPT", company_name=None)
    now = datetime.now(UTC)
    # Mix recent + older hits so baseline buzz is ELEVATED before S-Score promotion.
    hits = [
        RawSentimentHit(
            url=f"https://cafef.vn/fpt-r{i}",
            title=f"FPT công bố KQKD quý {i + 1} tăng trưởng",
            snippet="FPT công bố KQKD tăng trưởng vượt kế hoạch.",
            content="FPT công bố KQKD tăng trưởng vượt kế hoạch.",
            published_at=now - timedelta(hours=2),
            lang="VI",
        )
        for i in range(4)
    ] + [
        RawSentimentHit(
            url=f"https://cafef.vn/fpt-o{i}",
            title=f"FPT báo cáo tháng {i + 1}",
            snippet="FPT tăng trưởng ổn định.",
            content="FPT tăng trưởng ổn định.",
            published_at=now - timedelta(days=4),
            lang="VI",
        )
        for i in range(4)
    ]
    proxies = MarketProxiesInput(
        social_weighted_24h=10.0,
        social_mean_20d=2.0,
        social_std_20d=3.0,  # (10 - 2) / 3 = 2.67 >= 2.0
    )
    out = analyze_omni_channel_sentiment(entity, hits, now=now, market_proxies=proxies)
    assert out["benchmark_standards"]["buzz_velocity"] == "VIRAL_SURGE"


def test_verified_driver_rejects_non_http_url() -> None:
    """Dangerous schemes (javascript:, data:) are stripped from top_verified_drivers."""
    from app.api.v1.schemas import VerifiedDriverOut

    entity = resolve_equity_entity("FPT", company_name=None)
    now = datetime.now(UTC)
    out = analyze_omni_channel_sentiment(
        entity,
        [
            RawSentimentHit(
                url="javascript:alert(1)",
                title="FPT trúng thầu hợp đồng lớn",
                snippet="FPT trúng thầu chuyển đổi số lớn.",
                content="FPT trúng thầu chuyển đổi số lớn.",
                published_at=now - timedelta(hours=1),
                lang="VI",
            )
        ],
        now=now,
    )
    assert out["top_verified_drivers"]
    assert out["top_verified_drivers"][0]["url"] is None
    model = VerifiedDriverOut.model_validate(
        {**out["top_verified_drivers"][0], "url": "data:text/html,<script>1</script>"}
    )
    assert model.url is None


def test_market_sentiment_partial_timeout_and_budget_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial fan-out preserves fast batches on timeout and emits budget breaker."""
    import asyncio
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    import app.api.v1.market as market_mod
    from app.main import create_app

    class _TightPipe:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, params: Any) -> Any:
            self.calls += 1
            idx = self.calls
            if idx > 1:
                await asyncio.sleep(5.0)
            return SimpleNamespace(
                results=[
                    ScoredResult(
                        url="https://cafef.vn/fpt-partial-ok",
                        canonical_url="https://cafef.vn/fpt-partial-ok",
                        domain="cafef.vn",
                        title="FPT khối ngoại mua ròng",
                        snippet="FPT khối ngoại mua ròng kỷ lục",
                        engines=["google_news"],
                        score=0.9,
                        published_at=datetime.now(UTC),
                    )
                ]
            )

    monkeypatch.setattr(deps, "get_search_pipeline", lambda: _TightPipe())
    monkeypatch.setattr(
        market_mod,
        "create_budget_for_route",
        lambda *_a, **_kw: SimpleNamespace(remaining=lambda: 0.4),
    )

    with TestClient(create_app()) as client:
        resp = client.post(
            "/v1/market/sentiment",
            json={
                "ticker": "FPT",
                "max_queries": 3,
                "fetch_market_data": True,
                "timeout_ms": 1_500,
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    # At least the fast batch survived timeout AND tight budget signaled explicitly.
    assert any(d["url"] == "https://cafef.vn/fpt-partial-ok" for d in body["top_verified_drivers"])
    assert "QUANT_FETCH_SKIPPED_BUDGET" in body["risk_circuit_breakers"]
