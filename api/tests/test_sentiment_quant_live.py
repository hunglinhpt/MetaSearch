"""Sentiment-quant live fetchers (docs/32 Wave 2, docs/40 §3) — real parse +
merge behavior over httpx.MockTransport; no live network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.retrieval.sentiment_quant import (
    OhlcvBarInput,
    _cafef_num,
    _extract_cafef_rows,
    _to_bil_vnd,
    compute_cmf,
    compute_mfi,
    compute_realized_vol_annualized,
    fetch_cafef_vn_flows,
    fetch_cboe_daily_pcr,
    fetch_cnn_fear_greed,
    fetch_dnse_ohlcv_series,
    fetch_fred_hy_oas,
    fetch_institutional_sentiment_extras,
    fetch_live_market_proxies,
    fetch_sentiment_quant_context,
    fetch_simplize_extended,
    fetch_ssi_hose_breadth,
    fetch_ssi_iboard_orderflow,
    fetch_vn_benchmark,
)


def _mock(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _bars(n: int, base_close: float = 100.0) -> list[OhlcvBarInput]:
    return [
        OhlcvBarInput(
            time=1700000000 + i * 86400,
            open=base_close,
            high=base_close * 1.02,
            low=base_close * 0.98,
            close=base_close + i,
            volume=1_000_000.0,
        )
        for i in range(n)
    ]


# --- pure metric edge cases -------------------------------------------------------


def test_mfi_cmf_vol_edge_cases():
    bars = _bars(30)
    assert compute_mfi(bars[:5]) is None  # < period + 1
    assert compute_mfi(bars, period=1) is None
    flat = [OhlcvBarInput(time=i, open=1, high=1, low=1, close=1, volume=1) for i in range(1, 20)]
    assert compute_mfi(flat) is None  # no up/down flow at all
    assert compute_mfi(bars, period=14) is not None
    assert compute_cmf(bars[:10]) is None  # < period
    zero_vol = [
        OhlcvBarInput(time=i, open=1, high=2, low=1, close=1, volume=0) for i in range(1, 30)
    ]
    assert compute_cmf(zero_vol) is None
    assert compute_cmf(bars) is not None
    assert compute_realized_vol_annualized([100.0] * 5, window=10) is None
    assert compute_realized_vol_annualized([100.0, 101.0, 99.0, 102.0], window=2) is not None


def test_cafef_row_helpers():
    assert _to_bil_vnd(2.5e10) == 25.0  # raw VND -> billions
    assert _to_bil_vnd(1.5) == 1.5  # already in billions
    assert _to_bil_vnd(None) is None
    assert _extract_cafef_rows({"Data": {"Data": [{"Ngay": "x"}]}}) == [{"Ngay": "x"}]
    assert _extract_cafef_rows({"Data": {"ListDataTuDoanh": [{"a": 1}]}}) == [{"a": 1}]
    assert _extract_cafef_rows("nope") == []
    assert _cafef_num("1,234.5") == 1234.5
    assert _cafef_num("--") is None
    assert _cafef_num(None) is None


# --- CNN Fear & Greed -------------------------------------------------------------


def _cnn_payload():
    return {
        "fear_and_greed": {
            "score": 38.4,
            "rating": "fear",
            "previous_close": 40.1,
            "previous_1_week": 44.0,
            "previous_1_month": 51.2,
            "previous_1_year": 60.0,
            "timestamp": "2026-11-20T09:00:00",
        },
        "market_momentum_sp500": {"score": 33.0, "rating": "fear"},
        "stock_price_strength": {"score": 41.7, "rating": "neutral"},
        "junk_bond_demand": "not-a-dict",
    }


@pytest.mark.asyncio
async def test_cnn_fear_greed_happy_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "fearandgreed" in str(request.url)
        return httpx.Response(200, json=_cnn_payload())

    data = await fetch_cnn_fear_greed(client=_mock(handler))
    assert data is not None
    assert data["score"] == 38.4
    assert data["rating"] == "fear"
    assert data["previous_1_week"] == 44.0
    assert data["sub_indicators"]["market_momentum_sp500"]["score"] == 33.0
    assert "junk_bond_demand" not in data["sub_indicators"]
    assert data["source_name"].startswith("CNN")


@pytest.mark.asyncio
async def test_cnn_fear_greed_failures_return_none():
    assert await fetch_cnn_fear_greed(client=_mock(lambda r: httpx.Response(503))) is None
    assert (
        await fetch_cnn_fear_greed(client=_mock(lambda r: httpx.Response(200, json={"x": 1})))
        is None
    )
    assert (
        await fetch_cnn_fear_greed(
            client=_mock(lambda r: httpx.Response(200, json={"fear_and_greed": {}}))
        )
        is None
    )


# --- CBOE daily put/call ratio ----------------------------------------------------


def _cboe_payload():
    return {
        "ratios": [
            {"name": "Total Options Volume Ratio", "value": 1.02},
            {"name": "Equity Options Volume Ratio", "value": 0.88},
            {"name": "Index Options Volume Ratio", "value": 1.35},
            {"name": "garbage", "value": "nope"},
        ]
    }


@pytest.mark.asyncio
async def test_cboe_daily_pcr_happy_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "_daily_options" in str(request.url)
        return httpx.Response(200, json=_cboe_payload())

    data = await fetch_cboe_daily_pcr(client=_mock(handler))
    assert data is not None
    assert data["total_put_call_ratio"] == 1.02
    assert data["equity_put_call_ratio"] == 0.88
    assert data["index_put_call_ratio"] == 1.35


@pytest.mark.asyncio
async def test_cboe_daily_pcr_steps_back_shared_pool(monkeypatch):
    """With the shared pool (client=None) a missing day steps back a day."""
    import app.retrieval.sentiment_quant as sq

    today = datetime.now(UTC).date()
    today_str = today.strftime("%Y-%m-%d")
    yday_str = (today - timedelta(days=1)).strftime("%Y-%m-%d")

    def handler(request: httpx.Request) -> httpx.Response:
        if today_str in str(request.url):
            return httpx.Response(404)
        assert yday_str in str(request.url)
        return httpx.Response(200, json=_cboe_payload())

    monkeypatch.setattr(sq, "get_http_client", lambda *a, **kw: _mock(handler))
    data = await fetch_cboe_daily_pcr(client=None)
    assert data is not None and data["as_of"] == yday_str


@pytest.mark.asyncio
async def test_cboe_daily_pcr_bad_payload_returns_none():
    assert (
        await fetch_cboe_daily_pcr(
            client=_mock(lambda r: httpx.Response(200, json={"ratios": "nope"}))
        )
        is None
    )
    # injected client = single attempt, no step-back on empty ratios
    assert (
        await fetch_cboe_daily_pcr(client=_mock(lambda r: httpx.Response(200, json={"ratios": []})))
        is None
    )


# --- FRED HY OAS ------------------------------------------------------------------


_FRED_CSV = "DATE,BAMLH0A0HYM2\n2026-11-18,3.10\n2026-11-19,3.05\n2026-11-20,.,\n"


@pytest.mark.asyncio
async def test_fred_hy_oas_happy_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "BAMLH0A0HYM2" in str(request.url)
        return httpx.Response(200, text=_FRED_CSV)

    data = await fetch_fred_hy_oas(client=_mock(handler))
    assert data is not None
    assert data["credit_spread_bps"] == 305.0  # last valid value 3.05 -> bps
    assert data["as_of"] == "2026-11-19"


@pytest.mark.asyncio
async def test_fred_hy_oas_failures():
    assert await fetch_fred_hy_oas(client=_mock(lambda r: httpx.Response(500))) is None
    assert (
        await fetch_fred_hy_oas(client=_mock(lambda r: httpx.Response(200, text="DATE,OTHER\n")))
        is None
    )
    assert (
        await fetch_fred_hy_oas(
            client=_mock(lambda r: httpx.Response(200, text="BAMLH0A0HYM2,.\n"))
        )
        is None
    )  # header mention but no valid numeric row


# --- live market proxies ----------------------------------------------------------


def _yahoo_chart(close: float, n: int = 1):
    return {
        "chart": {
            "result": [
                {
                    "timestamp": [1700000000 + i * 86400 for i in range(n)],
                    "indicators": {
                        "quote": [
                            {
                                "open": [close] * n,
                                "high": [close] * n,
                                "low": [close] * n,
                                "close": [close] * n,
                                "volume": [1000.0] * n,
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


@pytest.mark.asyncio
async def test_live_market_proxies_vietnam_short_circuits():
    assert (
        await fetch_live_market_proxies(
            "FPT", is_vietnam=True, client=_mock(lambda r: httpx.Response(200))
        )
        is None
    )
    assert (
        await fetch_live_market_proxies(
            "not a symbol!", is_vietnam=False, client=_mock(lambda r: httpx.Response(200))
        )
        is None
    )


@pytest.mark.asyncio
async def test_live_market_proxies_happy_path():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "%5EVIX3M" in url:
            return httpx.Response(200, json=_yahoo_chart(21.0, n=2))
        if "%5EVIX" in url:
            return httpx.Response(200, json=_yahoo_chart(22.5, n=60))
        if "%5EVIX3M" in url:
            return httpx.Response(200, json=_yahoo_chart(21.0, n=2))
        if "%5ESKEW" in url:
            return httpx.Response(200, json=_yahoo_chart(145.2, n=2))
        if "cboe.com" in url:
            return httpx.Response(200, json=_cboe_payload())
        assert "fred" in url
        return httpx.Response(200, text=_FRED_CSV)

    proxies = await fetch_live_market_proxies(
        "NVDA",
        is_vietnam=False,
        client=_mock(handler),
        yahoo_chart_url="https://test/yahoo/v8/finance/chart",
    )
    assert proxies is not None
    assert proxies.vix == 22.5
    assert proxies.vix_ma50 is not None  # 60 bars -> 50-day MA available
    assert proxies.vix_3m == 21.0
    assert proxies.cboe_skew == 145.2
    assert proxies.cboe_put_call_equity == 0.88
    assert proxies.credit_spread_bps == 305.0
    assert "FRED HY OAS" in proxies.source_note and "^SKEW" in proxies.source_note


@pytest.mark.asyncio
async def test_live_market_proxies_all_dead_returns_none():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500))
    ) as dead_client:
        assert (
            await fetch_live_market_proxies(
                "NVDA",
                is_vietnam=False,
                client=dead_client,
                yahoo_chart_url="https://test/yahoo/v8/finance/chart",
            )
            is None
        )


# --- DNSE OHLCV -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dnse_ohlcv_happy_and_invalid_symbol():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["symbol"] == "FPT"
        ts = [1700000000 + i * 86400 for i in range(6)]
        return httpx.Response(
            200,
            json={
                "t": ts,
                "o": [20.0] * 6,
                "h": [21.0] * 6,
                "l": [19.0] * 6,
                "c": [20.5 + i * 0.1 for i in range(6)],
                "v": [100_000.0] * 6,
            },
        )

    bars, as_of = await fetch_dnse_ohlcv_series("FPT", client=_mock(handler))
    assert len(bars) == 6 and as_of is not None
    assert bars[-1].close == pytest.approx(21.0)
    assert await fetch_dnse_ohlcv_series("BAD!!", client=_mock(lambda r: httpx.Response(200))) == (
        [],
        None,
    )
    assert await fetch_dnse_ohlcv_series(
        "FPT", client=_mock(lambda r: httpx.Response(200, json={}))
    ) == ([], None)


# --- CafeF flows ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cafef_vn_flows_happy_path():
    def handler(request: httpx.Request) -> httpx.Response:
        ep = (
            "GDKhoiNgoai"
            if "GDKhoiNgoai" in str(request.url)
            else ("GDTuDoanh" if "GDTuDoanh" in str(request.url) else "GDCoDong")
        )
        if ep == "GDKhoiNgoai":
            return httpx.Response(
                200,
                json={
                    "Data": [
                        {
                            "Ngay": "20/11/2026",
                            "GiaTriMuaRong": 2.5e10,
                            "KhoiLuongMuaRong": 1_000_000,
                        }
                    ]
                },
            )
        if ep == "GDTuDoanh":
            return httpx.Response(
                200,
                json={"Data": [{"KLTDMua": 5e9, "KLTDBan": 3e9}]},
            )
        return httpx.Response(
            200,
            json={
                "Data": [
                    {"LoaiGiaoDich": "Mua", "SoLuongCoPhieuThucHien": 100_000},
                    {"LoaiGiaoDich": "Bán", "SoLuongCoPhieuThucHien": 40_000},
                ]
            },
        )

    flows = await fetch_cafef_vn_flows("FPT", client=_mock(handler))
    assert "CafeF GDKhoiNgoai" in flows["sources"]
    assert flows["foreign_net_val_5d_bil"] == 25.0
    assert flows["foreign_net_vol_20d"] == 1_000_000
    assert flows["foreign_as_of"] == "20/11/2026"
    assert flows["prop_net_val_5d_bil"] == 2.0  # (5e9 - 3e9) -> 2 bil
    assert flows["insider_buy_shares"] == 100_000
    assert flows["insider_sell_shares"] == 40_000
    assert flows["insider_mspr"] == pytest.approx(42.86)


@pytest.mark.asyncio
async def test_cafef_vn_flows_invalid_symbol_returns_empty():
    assert await fetch_cafef_vn_flows("NOPE!!", client=_mock(lambda r: httpx.Response(200))) == {}


# --- SSI iBoard order flow & breadth ----------------------------------------------


@pytest.mark.asyncio
async def test_ssi_iboard_orderflow_happy_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "iboard-query.ssi.com.vn/stock/FPT" in str(request.url)
        return httpx.Response(
            200,
            json={
                "data": {
                    "stockBUVol": 300_000.0,
                    "stockSDVol": 100_000.0,
                    "buyForeignQtty": 500_000.0,
                    "sellForeignQtty": 120_000.0,
                    "totalRoom": 10_000_000.0,
                    "currentRoom": 2_500_000.0,
                    "matchedPrice": 21.3,
                }
            },
        )

    flow = await fetch_ssi_iboard_orderflow("FPT", client=_mock(handler))
    assert flow["active_buy_ratio"] == 0.75
    assert flow["active_buy_vol"] == 300_000
    assert flow["foreign_remaining_room_pct"] == 25.0
    assert flow["matched_price"] == 21.3
    assert flow["source_name"] == "SSI iBoard"


@pytest.mark.asyncio
async def test_ssi_iboard_orderflow_list_node_and_failures():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"stockBUVol": 10, "stockSDVol": 30, "matchedPrice": 12.0}]}
        )

    flow = await fetch_ssi_iboard_orderflow("FPT", client=_mock(handler))
    assert flow["active_buy_ratio"] == 0.25
    assert flow["foreign_remaining_room_pct"] is None
    assert (
        await fetch_ssi_iboard_orderflow("FPT", client=_mock(lambda r: httpx.Response(503))) == {}
    )
    # empty dict node -> all metrics None (never fabricated)
    empty = await fetch_ssi_iboard_orderflow(
        "FPT", client=_mock(lambda r: httpx.Response(200, json={"data": {}}))
    )
    assert empty["active_buy_ratio"] is None and empty["matched_price"] is None


def _hose_rows():
    rows = []
    for i in range(3):  # advancing (first hits ceiling)
        rows.append(
            {
                "refPrice": 100.0,
                "matchedPrice": 110.0 if i == 0 else 101.0,
                "ceiling": 110.0,
                "floor": 90.0,
                "nmTotalTradedQty": 50_000,
            }
        )
    rows.append(
        {
            "refPrice": 100.0,
            "matchedPrice": 90.0,
            "ceiling": 110.0,
            "floor": 90.0,
            "nmTotalTradedQty": 10_000,
        }
    )
    rows.append(
        {
            "refPrice": 100.0,
            "matchedPrice": 100.0,
            "ceiling": 110.0,
            "floor": 90.0,
            "nmTotalTradedQty": 1_000,
        }
    )
    rows.append({"refPrice": None, "matchedPrice": None})  # skipped row
    return rows


@pytest.mark.asyncio
async def test_ssi_hose_breadth_happy_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "exchange/hose" in str(request.url)
        return httpx.Response(200, json={"data": _hose_rows()})

    breadth = await fetch_ssi_hose_breadth(client=_mock(handler))
    assert breadth is not None
    assert breadth["advancing_issues"] == 3
    assert breadth["declining_issues"] == 1
    assert breadth["unchanged_issues"] == 1
    assert breadth["ceiling_count"] == 1 and breadth["floor_count"] == 1
    # TRIN = (3/1) / (150k adv vol / 10k dec vol) and RANA = 1000 * (3-1)/4
    assert breadth["arms_trin"] == pytest.approx(0.2)
    assert breadth["mcclellan_rana"] == 500.0


@pytest.mark.asyncio
async def test_ssi_hose_breadth_failures_return_none():
    assert await fetch_ssi_hose_breadth(client=_mock(lambda r: httpx.Response(500))) is None
    assert (
        await fetch_ssi_hose_breadth(client=_mock(lambda r: httpx.Response(200, json={"data": []})))
        is None
    )
    bad_rows = [{"refPrice": 1.0, "matchedPrice": "x"}]
    assert (
        await fetch_ssi_hose_breadth(
            client=_mock(lambda r: httpx.Response(200, json={"data": bad_rows}))
        )
        is None
    )


# --- VN benchmark -----------------------------------------------------------------


def _vndirect_payload(n: int = 25, last: float = 1280.0, step: float = 2.0):
    ts = [1700000000 + i * 86400 for i in range(n)]
    closes = [last - (n - 1 - i) * step for i in range(n)]
    return {
        "s": "ok",
        "t": ts,
        "o": [c - 1 for c in closes],
        "h": [c + 5 for c in closes],
        "l": [c - 5 for c in closes],
        "c": closes,
        "v": [1_000_000.0] * n,
    }


@pytest.mark.asyncio
async def test_vn_benchmark_happy_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "symbol=VNINDEX" in str(request.url)
        return httpx.Response(200, json=_vndirect_payload(n=25, last=1280.0))

    bench = await fetch_vn_benchmark(client=_mock(handler))
    assert bench is not None
    assert bench["index"] == "VNINDEX"
    assert bench["last_close"] == 1280.0
    assert bench["ret_1d_pct"] == pytest.approx(round((1280.0 / 1278.0 - 1) * 100, 2))
    assert bench["ret_20d_pct"] is not None  # 25 bars -> 20d return available


@pytest.mark.asyncio
async def test_vn_benchmark_failures():
    assert await fetch_vn_benchmark(client=_mock(lambda r: httpx.Response(503))) is None
    assert (
        await fetch_vn_benchmark(
            client=_mock(lambda r: httpx.Response(200, json={"s": "ok", "c": [1.0, 2.0]}))
        )
        is None
    )  # too-short series refused


# --- Simplize extended (news + analyst consensus) ---------------------------------


@pytest.mark.asyncio
async def test_simplize_extended_happy_path():
    def handler(request: httpx.Request) -> httpx.Response:
        if "list-news" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "title": "FPT ký hợp đồng AI lớn",
                            "slug": "fpt-ai-deal",
                            "ts": 1760000000000,
                            "aiContent": "Giá trị hợp đồng 10 triệu USD",
                            "sourceNameDisplay": "Simplize News",
                        }
                    ]
                },
            )
        assert "analysis-report" in str(request.url)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "recommend": "BUY",
                        "targetPrice": 30.0,
                        "source": "VCBS",
                        "issueDate": "2026-11-10",
                    },
                    {"recommend": "MUA", "targetPrice": 32.0},
                    {"recommend": "SELL", "targetPrice": 25.0},
                ]
            },
        )

    ext = await fetch_simplize_extended("FPT", spot_price=25_000.0, client=_mock(handler))
    assert ext["news_hits"][0]["url"] == "https://simplize.vn/tin-tuc/fpt-ai-deal"
    assert ext["news_hits"][0]["published_at"] is not None
    cons = ext["analyst_consensus"]
    assert cons["buy_count"] == 2 and cons["sell_count"] == 1
    assert cons["consensus_rating"] == "HOLD"  # net (2-1)/3 = 0.33 < 0.4 BUY threshold
    # spot 25,000 VND with a 30 (thousand-VND) target -> x1000 correction
    assert cons["avg_target_price"] == pytest.approx(29_000.0)
    assert cons["upside_pct"] == pytest.approx(round((29_000 / 25_000 - 1) * 100, 2))


@pytest.mark.asyncio
async def test_simplize_extended_invalid_symbol():
    assert await fetch_simplize_extended("!!", client=_mock(lambda r: httpx.Response(200))) == {}


# --- institutional extras orchestrator ---------------------------------------------


@pytest.mark.asyncio
async def test_institutional_extras_vietnam_merges_everything():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "fearandgreed" in url:
            return httpx.Response(200, json=_cnn_payload())
        if "GDKhoiNgoai" in url or "GDTuDoanh" in url or "GDCoDong" in url:
            return httpx.Response(200, json={"Data": []})
        if "exchange/hose" in url:  # must precede the generic stock check
            return httpx.Response(200, json={"data": _hose_rows()})
        if "iboard-query.ssi.com.vn/stock/" in url:
            return httpx.Response(
                200, json={"data": {"stockBUVol": 10.0, "stockSDVol": 5.0, "matchedPrice": 20.0}}
            )
        if "dchart-api.vndirect.com.vn" in url:
            return httpx.Response(200, json=_vndirect_payload(n=10, last=1280.0))
        if "list-news" in url:
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"data": []})

    extras = await fetch_institutional_sentiment_extras(
        "FPT", is_vietnam=True, client=_mock(handler)
    )
    assert extras["cnn_fear_greed"]["score"] == 38.4
    assert extras["market_breadth"]["advancing_issues"] == 3
    assert extras["institutional_flows"] is not None
    assert "active_buy_ratio" in extras["institutional_flows"]
    assert extras["institutional_flows"]["vnindex_benchmark"]["last_close"] == 1280.0
    assert extras["analyst_consensus"] is None  # no reports served
    assert extras["direct_news_hits"] == []


@pytest.mark.asyncio
async def test_institutional_extras_vietnam_empty_flows_become_none():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(503))
    ) as dead_client:
        extras = await fetch_institutional_sentiment_extras(
            "FPT", is_vietnam=True, client=dead_client
        )
    assert extras["institutional_flows"] is None
    assert extras["market_breadth"] is None


@pytest.mark.asyncio
async def test_institutional_extras_global_us_macro_breadth():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "fearandgreed" in url:
            return httpx.Response(200, json=_cnn_payload())
        if "cboe.com" in url:
            return httpx.Response(200, json=_cboe_payload())
        assert "fred" in url
        return httpx.Response(200, text=_FRED_CSV)

    extras = await fetch_institutional_sentiment_extras(
        "NVDA", is_vietnam=False, client=_mock(handler)
    )
    breadth = extras["market_breadth"]
    assert breadth["exchange"] == "US_MACRO"
    assert breadth["cnn_fear_greed_score"] == 38.4
    assert breadth["cboe_equity_put_call_ratio"] == 0.88
    assert breadth["fred_hy_oas_bps"] == 305.0
    assert extras["institutional_flows"] is None
    assert extras["analyst_consensus"] is None


# --- quant context orchestration ---------------------------------------------------


@pytest.mark.asyncio
async def test_sentiment_quant_context_vndirect_primary():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "dchart" in str(request.url) and "symbol=FPT" in str(request.url)
        # stock closes < 1000 -> x1000 multiplier applied by the shared parser
        return httpx.Response(200, json=_vndirect_payload(n=25, last=21.25, step=0.05))

    bars, src, src_url, _as_of, _fa = await fetch_sentiment_quant_context(
        "FPT", is_vietnam=True, client=_mock(handler)
    )
    assert src == "VNDIRECT"
    assert len(bars) == 25
    assert bars[-1].close == pytest.approx(21_250.0)
    assert _as_of is not None and src_url


@pytest.mark.asyncio
async def test_sentiment_quant_context_invalid_symbol():
    assert await fetch_sentiment_quant_context("BAD!!", is_vietnam=True, client=None) == (
        [],
        None,
        None,
        None,
        None,
    )


@pytest.mark.asyncio
async def test_sentiment_quant_context_yahoo_for_global():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/chart/NVDA" in str(request.url)
        return httpx.Response(200, json=_yahoo_chart(180.0, n=30))

    bars, src, _url, _as_of, _fa = await fetch_sentiment_quant_context(
        "NVDA",
        is_vietnam=False,
        client=_mock(handler),
        yahoo_chart_url="https://test/yahoo/v8/finance/chart",
    )
    assert src == "Yahoo Finance"
    assert len(bars) == 30 and bars[-1].close == 180.0
    assert _fa is None  # FA snapshot only for Vietnam
