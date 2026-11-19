"""Wave 2 tests: v4.0 Institutional Sentiment Engine v3 (live keyless feeds with
MockTransport, SEntFiN attribution, math fixes, regimes, hybrid blending, Alembic 0011 S-Score).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from fastapi.testclient import TestClient

from app.main import create_app
from app.retrieval.sentiment import (
    RawSentimentHit,
    analyze_omni_channel_sentiment,
    resolve_equity_entity,
    run_llm_sentiment_judge,
)
from app.retrieval.sentiment_quant import (
    MarketProxiesInput,
    OhlcvBarInput,
    compute_market_proxy_metrics,
    compute_ohlcv_sentiment_metrics,
    fetch_cafef_vn_flows,
    fetch_cboe_daily_pcr,
    fetch_cnn_fear_greed,
    fetch_dnse_ohlcv_series,
    fetch_fred_hy_oas,
    fetch_sentiment_quant_context,
    fetch_simplize_extended,
    fetch_ssi_hose_breadth,
    fetch_ssi_iboard_orderflow,
    fetch_vn_benchmark,
    load_sentiment_history_s_score,
    record_sentiment_snapshot,
)


def _mock(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestSentimentMathFixes:
    def test_ohlcv_down_day_low_volume_never_inverts_to_positive_surge(self):
        # 25 bars: last bar drops -2.5% on thin volume (0.5x avg) -> flow score must not exceed
        # a mild down-day on high volume, and volatile Z-returns stay finite.
        bars = [
            OhlcvBarInput(
                time=1760000000 + i * 86400,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1_000_000.0,
            )
            for i in range(24)
        ]
        thin_drop = [
            *bars,
            OhlcvBarInput(
                time=1760000000 + 24 * 86400,
                open=100.0,
                high=100.5,
                low=97.0,
                close=97.5,
                volume=500_000.0,
            ),
        ]
        heavy_drop = [
            *bars,
            OhlcvBarInput(
                time=1760000000 + 24 * 86400,
                open=100.0,
                high=100.5,
                low=97.0,
                close=97.5,
                volume=2_000_000.0,
            ),
        ]
        m_thin = compute_ohlcv_sentiment_metrics(thin_drop)
        m_heavy = compute_ohlcv_sentiment_metrics(heavy_drop)
        assert m_thin["return_5d_pct"] == -2.5
        assert m_thin["return_5d_z"] is not None and m_thin["return_5d_z"] < 0
        # High-volume selloff must score strictly lower (more bearish) than thin-volume drop
        assert m_heavy["breadth_flow_quant_score"] < m_thin["breadth_flow_quant_score"]

    def test_bofa_indicator_requires_two_independent_signal_groups(self):
        # Single group (only volatility/options: vix + vix_3m) must return None!
        single = compute_market_proxy_metrics(MarketProxiesInput(vix=18.0, vix_3m=20.0))
        assert single["bofa_bull_bear_analog_0_10"] is None

        # Two independent groups (vol/options + breadth) -> populated cleanly
        multi = compute_market_proxy_metrics(
            MarketProxiesInput(vix=18.0, vix_3m=20.0, advancing_issues=280, declining_issues=110)
        )
        assert multi["bofa_bull_bear_analog_0_10"] is not None


class TestVerifiedKeylessConnectors:
    async def test_cnn_cboe_fred_global_connectors(self):
        def handler(req: httpx.Request) -> httpx.Response:
            u = str(req.url)
            if "dataviz.cnn.io" in u:
                return httpx.Response(
                    200,
                    json={
                        "fear_and_greed": {
                            "score": 63.4,
                            "rating": "greed",
                            "timestamp": "2026-11-19T00:00:00Z",
                            "previous_close": 60.1,
                            "previous_1_week": 52.0,
                            "previous_1_month": 44.0,
                            "previous_1_year": 58.0,
                        },
                        "market_momentum_sp500": {"score": 71.0, "rating": "greed"},
                    },
                )
            if "cdn.cboe.com" in u:
                return httpx.Response(
                    200,
                    json={
                        "ratios": [
                            {"name": "TOTAL PUT/CALL RATIO", "value": "0.84"},
                            {"name": "EQUITY PUT/CALL RATIO", "value": "0.62"},
                            {"name": "INDEX PUT/CALL RATIO", "value": "1.12"},
                        ]
                    },
                )
            return httpx.Response(
                200,
                text="observation_date,BAMLH0A0HYM2\n2026-11-17,3.12\n2026-11-18,3.05\n",
            )

        cli = _mock(handler)
        cnn = await fetch_cnn_fear_greed(client=cli)
        pcr = await fetch_cboe_daily_pcr(client=cli)
        oas = await fetch_fred_hy_oas(client=cli)
        assert cnn is not None and cnn["score"] == 63.4
        assert cnn["sub_indicators"]["market_momentum_sp500"]["score"] == 71.0
        assert pcr is not None and pcr["equity_put_call_ratio"] == 0.62
        assert oas is not None and oas["credit_spread_bps"] == 305.0

    async def test_cafef_ssi_dnse_vnindex_simplize_connectors(self):
        def handler(req: httpx.Request) -> httpx.Response:
            u = str(req.url)
            if "GDKhoiNgoai" in u:
                return httpx.Response(
                    200,
                    json={
                        "Data": {
                            "Data": [
                                {
                                    "Ngay": "19/11/2026",
                                    "GiaTriMuaRong": 55_000_000_000,
                                    "KhoiLuongMuaRong": 400000,
                                },
                                {
                                    "Ngay": "18/11/2026",
                                    "GiaTriMuaRong": 25_000_000_000,
                                    "KhoiLuongMuaRong": 200000,
                                },
                            ]
                        }
                    },
                )
            if "GDTuDoanh" in u:
                return httpx.Response(
                    200,
                    json={
                        "Data": {
                            "Data": {
                                "ListDataTuDoanh": [
                                    {"Ngay": "19/11/2026", "GiaTriMuaRong": 15_000_000_000},
                                ]
                            }
                        }
                    },
                )
            if "GDCoDong" in u:
                return httpx.Response(
                    200,
                    json={
                        "Data": {
                            "Data": [
                                {"LoaiGiaoDich": "Mua", "SoLuongCoPhieuThucHien": 300_000},
                                {"LoaiGiaoDich": "Bán", "SoLuongCoPhieuThucHien": 100_000},
                            ]
                        }
                    },
                )
            if "iboard-query.ssi.com.vn/stock/exchange/hose" in u:
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "refPrice": 20.0,
                                "matchedPrice": 21.0,
                                "nmTotalTradedQty": 2_000_000,
                                "ceiling": 21.4,
                                "floor": 18.6,
                            },
                            {
                                "refPrice": 30.0,
                                "matchedPrice": 31.5,
                                "nmTotalTradedQty": 1_500_000,
                                "ceiling": 32.1,
                                "floor": 27.9,
                            },
                            {
                                "refPrice": 40.0,
                                "matchedPrice": 39.0,
                                "nmTotalTradedQty": 500_000,
                                "ceiling": 42.8,
                                "floor": 37.2,
                            },
                        ]
                    },
                )
            if "iboard-query.ssi.com.vn/stock/FPT" in u:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "stockBUVol": 1_200_000,
                            "stockSDVol": 400_000,
                            "buyForeignQtty": 350_000,
                            "sellForeignQtty": 50_000,
                            "totalRoom": 100_000_000,
                            "currentRoom": 12_000_000,
                            "matchedPrice": 136500,
                        }
                    },
                )
            if "services.entrade.com.vn" in u or "dchart-api.vndirect.com.vn" in u:
                now_ts = 1763500000
                return httpx.Response(
                    200,
                    json={
                        "s": "ok",
                        "t": [now_ts - (24 - i) * 86400 for i in range(25)],
                        "o": [1250.0 + i for i in range(25)],
                        "h": [1255.0 + i for i in range(25)],
                        "l": [1245.0 + i for i in range(25)],
                        "c": [1252.0 + i for i in range(25)],
                        "v": [1_000_000.0] * 25,
                    },
                )
            if "list-news" in u:
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "title": "FPT mở trung tâm AI",
                                "slug": "fpt-ai",
                                "ts": 1763452800000,
                                "sourceName": "Simplize",
                            }
                        ]
                    },
                )
            if "analysis-report/list" in u:
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "source": "SSI",
                                "recommend": "BUY",
                                "targetPrice": 165000,
                                "issueDate": "2026-11-18",
                            },
                            {
                                "source": "HSC",
                                "recommend": "MUA",
                                "targetPrice": 175000,
                                "issueDate": "2026-11-15",
                            },
                        ]
                    },
                )
            return httpx.Response(404)

        cli = _mock(handler)
        flows = await fetch_cafef_vn_flows("FPT", client=cli)
        assert flows["foreign_net_val_5d_bil"] == 80.0
        assert flows["prop_net_val_5d_bil"] == 15.0
        assert flows["insider_mspr"] == 50.0

        ssi_of = await fetch_ssi_iboard_orderflow("FPT", client=cli)
        assert ssi_of["active_buy_ratio"] == 0.75
        assert ssi_of["foreign_remaining_room_pct"] == 12.0

        breadth = await fetch_ssi_hose_breadth(client=cli)
        assert breadth is not None
        assert breadth["advancing_issues"] == 2 and breadth["declining_issues"] == 1
        assert breadth["arms_trin"] is not None
        assert breadth["mcclellan_rana"] is not None

        dnse_bars, _ = await fetch_dnse_ohlcv_series("FPT", client=cli)
        assert len(dnse_bars) == 25

        vn_idx = await fetch_vn_benchmark(client=cli)
        assert vn_idx is not None and vn_idx["ret_20d_pct"] > 0

        simp = await fetch_simplize_extended("FPT", spot_price=136500.0, client=cli)
        assert len(simp["news_hits"]) == 1
        assert simp["analyst_consensus"]["consensus_rating"] == "BUY"
        assert simp["analyst_consensus"]["avg_target_price"] == 170000.0
        assert simp["analyst_consensus"]["upside_pct"] > 20.0

        # DNSE fallback inside fetch_sentiment_quant_context when VNDIRECT 404s
        def handler_no_vnd(req: httpx.Request) -> httpx.Response:
            if "dchart-api.vndirect.com.vn" in str(req.url):
                return httpx.Response(503)
            return handler(req)

        bars, src, _u, _as_of, _fa = await fetch_sentiment_quant_context(
            "FPT", is_vietnam=True, client=_mock(handler_no_vnd)
        )
        assert src == "DNSE" and len(bars) == 25


class TestSentimentNlpV3AndEndpoint:
    def test_wrapper_domain_resolution_sentfin_and_negation(self):
        now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
        entity_fpt = resolve_equity_entity("FPT", now=now)

        # Hit 1: Google News wrapper pointing via source_url to cafef.vn -> TIER_2
        # Body praises VCB sharply, while saying FPT misses growth / hạ khuyến nghị
        multi_entity_hit = RawSentimentHit(
            url="https://news.google.com/rss/articles/CBMiTest",
            source_url="https://cafef.vn/vcb-fpt-q3.chn",
            title="Cập nhật thị trường tài chính: VCB và FPT",
            snippet=(
                "VCB ghi nhận lợi nhuận kỷ lục và khối ngoại mua ròng mạnh mẽ vượt kỳ vọng. "
                "Trong khi đó FPT gặp hạ khuyến nghị và suy giảm lợi nhuận ngắn hạn."
            ),
            published_at=now - timedelta(hours=1),
            lang="VI",
        )
        out = analyze_omni_channel_sentiment(entity_fpt, [multi_entity_hit], now=now)
        assert out["network_telemetry"]["official_and_media"]["hit_count"] == 1
        assert out["top_verified_drivers"][0]["tier"] == "TIER_2"
        assert out["top_verified_drivers"][0]["platform_or_domain"] == "cafef.vn"
        # FPT's sentence is negative while VCB's positive sentence is attenuated -> bearish
        assert out["sentiment_summary"]["composite_score"] < -0.15

        # Rumor denial must NOT trigger UNVERIFIED_RUMOR_SPIKE
        denial_hit = RawSentimentHit(
            url="https://cafef.vn/fpt-dinh-chinh.chn",
            title="FPT chính thức bác bỏ tin đồn thất thiệt",
            snippet="Công ty FPT phát thông báo bác bỏ tin đồn và ghi nhận tăng trưởng hai chữ số.",
            published_at=now - timedelta(hours=2),
            lang="VI",
        )
        denial_out = analyze_omni_channel_sentiment(entity_fpt, [denial_hit], now=now)
        assert denial_out["social_integrity_audit"]["unverified_rumor_count"] == 0

    def test_bearish_markdown_and_hybrid_composite_score(self):
        now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
        entity = resolve_equity_entity("FPT", now=now)
        hits = [
            RawSentimentHit(
                url="https://cafef.vn/fpt-ban-rong.chn",
                title="FPT: khối ngoại xả ròng liên tục và hạ khuyến nghị",
                snippet="Áp lực bán tháo khiến FPT thủng MA200 với thanh khoản lớn.",
                published_at=now - timedelta(hours=1),
                lang="VI",
            ),
            RawSentimentHit(
                url="https://f319.com/threads/fpt.1",
                title="FPT bán tháo thủng đáy",
                snippet="Cổ đông FPT nằm sàn lo giải chấp.",
                published_at=now - timedelta(hours=2),
                lang="VI",
            ),
        ]
        # Pure text mode keeps text-only weighting
        text_only = analyze_omni_channel_sentiment(entity, hits, now=now)
        assert text_only["sentiment_summary"]["market_regime"] == "BEARISH_MARKDOWN"
        assert text_only["sentiment_summary"]["quant_score"] is None
        assert (
            text_only["sentiment_summary"]["text_score"]
            == text_only["sentiment_summary"]["composite_score"]
        )

        # Hybrid mode incorporates institutional flows & analyst consensus
        hybrid = analyze_omni_channel_sentiment(
            entity,
            hits,
            now=now,
            institutional_flows={"foreign_net_val_5d_bil": 150.0, "active_buy_ratio": 0.72},
            analyst_consensus={
                "buy_count": 5,
                "hold_count": 0,
                "sell_count": 0,
                "upside_pct": 30.0,
            },
        )
        assert hybrid["sentiment_summary"]["quant_score"] > 0.3
        assert (
            hybrid["sentiment_summary"]["composite_score"]
            > text_only["sentiment_summary"]["composite_score"]
        )
        assert hybrid["sentiment_summary"]["pillar_weights"] is not None

    def test_regime_not_binary_for_weak_unpolarized_and_confidence_penalizes_conflict(self):
        now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
        entity = resolve_equity_entity("FPT", now=now)
        bullish = [
            RawSentimentHit(
                url="https://cafef.vn/fpt-kqkd-1.chn",
                title="FPT lợi nhuận quý 3 tăng trưởng 24% vượt kỳ vọng",
                snippet="FPT ghi nhận doanh thu tăng trưởng mạnh và biên lợi nhuận mở rộng.",
                published_at=now - timedelta(hours=2),
                lang="VI",
            ),
            RawSentimentHit(
                url="https://vietstock.vn/fpt-khuyen-nghi-mua.htm",
                title="FPT được khuyến nghị mua với giá mục tiêu cao hơn",
                snippet="Các công ty chứng khoán nâng giá mục tiêu FPT nhờ tăng trưởng AI.",
                published_at=now - timedelta(hours=3),
                lang="VI",
            ),
        ]
        neutral_only = analyze_omni_channel_sentiment(
            entity,
            [
                RawSentimentHit(
                    url="https://cafef.vn/fpt-lich-hop.chn",
                    title="FPT thông báo lịch họp thường niên",
                    snippet="Công ty công bố thời gian tổ chức cuộc họp.",
                    published_at=now - timedelta(hours=2),
                    lang="VI",
                )
            ],
            now=now,
        )
        assert neutral_only["sentiment_summary"]["market_regime"] != "EVENT_DRIVEN_BINARY"

        aligned = analyze_omni_channel_sentiment(
            entity,
            bullish,
            now=now,
            institutional_flows={"foreign_net_val_5d_bil": 150.0, "active_buy_ratio": 0.72},
        )
        conflict = analyze_omni_channel_sentiment(
            entity,
            bullish,
            now=now,
            institutional_flows={"foreign_net_val_5d_bil": -150.0, "active_buy_ratio": 0.28},
        )
        assert (
            conflict["sentiment_summary"]["confidence"] < aligned["sentiment_summary"]["confidence"]
        )

    def test_route_accepts_brk_dot_b_and_btc_usd(self):
        client = TestClient(create_app())
        for sym in ("BRK.B", "BTC-USD"):
            resp = client.post(
                "/v1/market/sentiment",
                json={
                    "ticker": sym,
                    "fetch_market_data": False,
                    "retrieved_documents": [
                        {
                            "url": "https://reuters.com/markets/test",
                            "title": f"{sym} beats estimates with margin expansion",
                            "snippet": f"{sym} delivered strong earnings growth.",
                        }
                    ],
                },
            )
            assert resp.status_code == 200
            assert resp.json()["entity"]["ticker"] == sym

    async def test_snapshot_persistence_and_20d_s_score(self, tmp_path, monkeypatch):
        from app.config import get_settings
        from app.db.base import Base
        from app.db.session import dispose_engine, get_engine

        db_file = tmp_path / "sentiment_test.db"
        url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
        monkeypatch.setenv("DATABASE_URL", url)
        get_settings.cache_clear()
        await dispose_engine()
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            today = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
            # Fewer than 5 days -> insufficient history
            for d in range(1, 4):
                await record_sentiment_snapshot(
                    ticker="FPT",
                    market="VN",
                    composite_score=0.10,
                    text_score=0.10,
                    quant_score=None,
                    social_volume=5,
                    mention_count=8,
                    regime="MOMENTUM_MARKUP",
                    confidence=0.6,
                    as_of=today - timedelta(days=d),
                )
            low_hist = await load_sentiment_history_s_score(
                "FPT", current_score=0.5, current_volume=10, as_of=today
            )
            assert low_hist["available"] is False and low_hist["s_score"] is None

            # Top up to 6 prior days with slight variation -> valid 20d S-Score
            for d, sc, vol in ((4, 0.12, 6), (5, 0.08, 4), (6, 0.14, 7)):
                await record_sentiment_snapshot(
                    ticker="FPT",
                    market="VN",
                    composite_score=sc,
                    text_score=sc,
                    quant_score=None,
                    social_volume=vol,
                    mention_count=8,
                    regime="MOMENTUM_MARKUP",
                    confidence=0.6,
                    as_of=today - timedelta(days=d),
                )
            ok_hist = await load_sentiment_history_s_score(
                "FPT", current_score=0.65, current_volume=15, as_of=today
            )
            assert ok_hist["available"] is True
            assert ok_hist["history_days"] == 6
            assert ok_hist["s_score"] > 2.0
            assert ok_hist["sv_score"] > 2.0
        finally:
            await dispose_engine()
            get_settings.cache_clear()

    async def test_llm_sentiment_judge_opt_in_and_fail_open(self):
        entity = resolve_equity_entity("NVDA")
        assert await run_llm_sentiment_judge(None, entity, [{"tier": "TIER_1"}]) is None

        class _StubGW:
            provider = "openai"

            async def complete(self, _msgs, **_kw):
                return (
                    '{"narrative_summary": "Aligned institutional buildup", '
                    '"driver_alignment": "ALIGNED", "contrarian_risk": "LOW", '
                    '"score_adjustment": 0.42}',
                    None,
                )

        judged = await run_llm_sentiment_judge(
            _StubGW(),
            entity,
            [{"tier": "TIER_1", "polarity": "POSITIVE", "headline_or_post_summary": "NVDA beats"}],
        )
        assert judged is not None
        # Must clamp excessive adjustment safely into [-0.15, +0.15]
        assert judged["score_adjustment"] == 0.15
        assert judged["driver_alignment"] == "ALIGNED"

    def test_route_accepts_live_dataclass_wire_and_corporate_events(self, monkeypatch):
        from types import SimpleNamespace

        from app.api import deps
        from app.api.v1 import market as market_api
        from app.retrieval import events as events_mod
        from app.retrieval import sentiment_quant as sq_mod
        from app.retrieval.events import CorporateEventRecord, ParsedNewsItem

        market_api.clear_sentiment_cache()

        class _NoopSearchPipeline:
            async def run(self, _params):
                return SimpleNamespace(results=[])

        async def _fake_wire(**_kw):
            return {
                "items": [
                    ParsedNewsItem(
                        title="HPG báo lãi quý 4 tăng trưởng mạnh",
                        url="https://cafef.vn/hpg-live-dc.chn",
                        source_id="cafef_rss",
                        snippet="HPG doanh thu xuất khẩu thép tăng 28% YoY.",
                        published_at=datetime.now(UTC) - timedelta(hours=2),
                        symbols=["HPG"],
                    )
                ]
            }

        async def _fake_events(**_kw):
            ev = CorporateEventRecord(
                event_id="ev-hpg-1",
                symbol="HPG",
                exchange="HOSE",
                event_type="dividend_calendar",
                event_subtype="cash_dividend",
                severity="info",
                risk_level=1,
                title="Chốt quyền cổ tức tiền mặt HPG",
                ex_date="2026-12-05",
            )
            return {"events": [ev], "risk_alerts": [ev]}

        async def _fake_quant(*_a, **_kw):
            return [], "VNDIRECT", "https://dchart.vndirect.com.vn/?symbol=HPG", None, None

        async def _fake_extras(*_a, **_kw):
            return {}

        monkeypatch.setattr(deps, "get_search_pipeline", lambda: _NoopSearchPipeline())
        monkeypatch.setattr(events_mod, "fetch_market_news_wire", _fake_wire)
        monkeypatch.setattr(events_mod, "fetch_market_events", _fake_events)
        monkeypatch.setattr(sq_mod, "fetch_sentiment_quant_context", _fake_quant)
        monkeypatch.setattr(sq_mod, "fetch_institutional_sentiment_extras", _fake_extras)

        with TestClient(create_app()) as client:
            resp = client.post(
                "/v1/market/sentiment",
                json={
                    "ticker": "HPG",
                    "fetch_market_data": True,
                    "include_institutional_feeds": True,
                    "timeout_ms": 4000,
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["corporate_events"]["count"] == 1
            assert body["corporate_events"]["upcoming_or_recent"][0]["event_id"] == "ev-hpg-1"
            assert any(
                d.get("url") == "https://cafef.vn/hpg-live-dc.chn"
                for d in (body.get("top_verified_drivers") or [])
            )
        market_api.clear_sentiment_cache()
