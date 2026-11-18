"""Tests for Market News Wire & Corporate Events Intelligence (REST, parsers & MCP)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.core.auth import static_route_cost
from app.mcp import client as mcp_client
from app.retrieval.events import (
    CorporateEventRecord,
    ParsedNewsItem,
    classify_insider_trade,
    classify_listing_event,
    classify_regulatory_action,
    content_fingerprint,
    deduplicate_news_items,
    explain_share_ratio,
    extract_tickers_from_text,
    fetch_market_events,
    fetch_market_news_wire,
    filter_news_freshness,
    parse_cafef_corporate_actions,
    parse_cotuc_calendar_html,
    parse_html_article_links,
    parse_rss_feed_xml,
    parse_share_factor,
)
from app.retrieval.health import EngineBreakerRegistry


def test_context_aware_ticker_extraction_protects_macro_acronyms() -> None:
    fx_text = "Tỷ giá tăng nhẹ, khối ngoại rút ròng 100 USD và cặp USD/VND chạm 25.400 đồng"
    assert "USD" not in extract_tickers_from_text(fx_text)

    macro_text = "CPI tháng 10 tăng 0,2% trong khi GDP quý 3 đạt 6,8% và lãi suất OMO ổn định"
    extracted_macro = extract_tickers_from_text(macro_text)
    assert "CPI" not in extracted_macro
    assert "GDP" not in extracted_macro
    assert "OMO" not in extracted_macro

    explicit_prefix = "CPI: CTCP Đầu tư Phát triển Công nghiệp công bố nghị quyết HĐQT"
    assert "CPI" in extract_tickers_from_text(explicit_prefix)

    explicit_phrase = "Cổ phiếu TPP và mã HPG cùng tăng trần phiên sáng"
    syms = extract_tickers_from_text(explicit_phrase)
    assert "TPP" in syms
    assert "HPG" in syms

    global_text = "$NVDA posts strong AI data center revenue while $AAPL expands buyback"
    global_syms = extract_tickers_from_text(global_text)
    assert "NVDA" in global_syms
    assert "AAPL" in global_syms


def test_fingerprint_and_syndication_dedup() -> None:
    fp1 = content_fingerprint("14:30 | 18/11/2026 - FPT đạt lợi nhuận kỷ lục quý 4")
    fp2 = content_fingerprint("FPT đạt lợi nhuận kỷ lục quý 4 (08:05)")
    assert fp1 == fp2

    it1 = ParsedNewsItem(
        title="FPT báo lãi kỷ lục nhờ xuất khẩu phần mềm",
        url="https://cafef.vn/fpt-lai-ky-luc.chn?utm_source=rss",
        source_id="cafef_corporate",
        snippet="Ngắn",
        symbols=["FPT"],
        fingerprint=content_fingerprint("FPT báo lãi kỷ lục nhờ xuất khẩu phần mềm"),
    )
    it2 = ParsedNewsItem(
        title="FPT báo lãi kỷ lục nhờ xuất khẩu phần mềm (15:10)",
        url="https://vietstock.vn/fpt-lai-ky-luc.htm",
        source_id="vietstock_corporate",
        snippet="Bài phân tích chi tiết hơn về kết quả kinh doanh quý 4 của FPT Corp.",
        symbols=["FPT"],
        fingerprint=content_fingerprint("FPT báo lãi kỷ lục nhờ xuất khẩu phần mềm (15:10)"),
    )
    deduped = deduplicate_news_items([it1, it2])
    assert len(deduped) == 1
    merged = deduped[0]
    assert merged.duplicate_count == 2
    assert merged.also_reported_by == ["vietstock_corporate"]
    assert "chi tiết hơn" in merged.snippet


def test_dual_semantics_freshness_filtering() -> None:
    today = date(2026, 11, 18)
    stale_news = ParsedNewsItem(
        title="Tin cũ 20 ngày trước",
        url="https://example.com/1",
        source_id="rss",
        published_at=datetime(2026, 10, 29, 9, 0, tzinfo=UTC),
    )
    future_event = ParsedNewsItem(
        title="Thông báo chốt quyền GDKHQ",
        url="https://example.com/2",
        source_id="rss",
        published_at=datetime(2026, 10, 25, 9, 0, tzinfo=UTC),
        event_date="2026-11-25",
    )
    expired_event = ParsedNewsItem(
        title="Sự kiện đã qua ngày chốt",
        url="https://example.com/3",
        source_id="rss",
        event_date="2026-11-10",
    )
    undated = ParsedNewsItem(
        title="Tin chưa gắn mốc thời gian",
        url="https://example.com/4",
        source_id="html",
    )

    kept = filter_news_freshness(
        [stale_news, future_event, expired_event, undated],
        today=today,
        max_age_days=7,
    )
    titles = {it.title: it.date_status for it in kept}
    assert "Tin cũ 20 ngày trước" not in titles
    assert "Sự kiện đã qua ngày chốt" not in titles
    assert titles["Thông báo chốt quyền GDKHQ"] == "event_date"
    assert titles["Tin chưa gắn mốc thời gian"] == "undated"


def test_listing_regulatory_and_insider_classification() -> None:
    reinstated = classify_listing_event(
        "HOSE: Đưa cổ phiếu XYZ ra khỏi diện cảnh báo và giao dịch trở lại toàn thời gian"
    )
    assert reinstated.subtype == "reinstated"
    assert reinstated.is_risk_alert is False
    assert reinstated.severity == "info"

    delist = classify_listing_event("Quyết định hủy niêm yết bắt buộc cổ phiếu DEF")
    assert delist.subtype == "delisting"
    assert delist.is_risk_alert is True
    assert delist.severity == "critical"

    add_list = classify_listing_event(
        "Chấp thuận niêm yết bổ sung 12.000.000 cổ phiếu phát hành trả cổ tức"
    )
    assert add_list.subtype == "additional_listing"
    assert add_list.is_dilution is True
    assert add_list.shares_delta == pytest.approx(12_000_000.0)
    assert add_list.severity == "high"

    # Regulatory severity-first ordering beats incidental listing phrases.
    sanction = classify_regulatory_action(
        "UBCKNN xử phạt vi phạm hành chính do chậm công bố thông tin chấp thuận niêm yết"
    )
    assert sanction.subtype == "sanction"
    assert sanction.risk_level >= 3
    assert sanction.severity == "critical"

    stats = classify_regulatory_action("Thống kê giao dịch thị trường cổ phiếu tháng 10")
    assert stats.subtype == "statistics"

    sell_plan = classify_insider_trade("Phó Tổng Giám đốc đăng ký bán 2.5 triệu cổ phiếu")
    assert sell_plan.direction == "sell"
    assert sell_plan.execution_status == "planned"
    assert sell_plan.shares_count == pytest.approx(2_500_000.0)

    buy_done = classify_insider_trade("Cổ đông lớn đã mua hoàn tất 500.000 cổ phiếu")
    assert buy_done.direction == "buy"
    assert buy_done.execution_status == "executed"
    assert buy_done.shares_count == pytest.approx(500_000.0)


def test_share_factor_compound_and_ratio_explanation() -> None:
    assert parse_share_factor("Chi trả cổ tức bằng tiền mặt tỷ lệ 15%") is None

    compound = parse_share_factor(
        "Trả cổ tức bằng cổ phiếu tỷ lệ 100:23 và phát hành cổ phiếu thưởng tỷ lệ 100:18"
    )
    assert compound == pytest.approx(round(1.23 * 1.18, 4))

    ev1 = CorporateEventRecord(
        event_id="e1",
        symbol="FPT",
        exchange="HOSE",
        event_type="corporate_action",
        event_subtype="stock_dividend",
        severity="high",
        risk_level=1,
        title="Trả cổ tức bằng cổ phiếu tỷ lệ 100:23",
        event_date="2025-06-12",
        is_share_changing=True,
        share_factor=1.23,
    )
    ev2 = CorporateEventRecord(
        event_id="e2",
        symbol="FPT",
        exchange="HOSE",
        event_type="corporate_action",
        event_subtype="bonus_shares",
        severity="high",
        risk_level=1,
        title="Phát hành cổ phiếu thưởng tỷ lệ 100:18",
        event_date="2026-05-15",
        is_share_changing=True,
        share_factor=1.18,
    )

    single = explain_share_ratio([ev1, ev2], 1.23)
    assert single.explained is True
    assert single.matched_factor == pytest.approx(1.23)
    assert len(single.matched_events) == 1

    chained = explain_share_ratio([ev1, ev2], 1.4514)
    assert chained.explained is True
    assert chained.matched_factor == pytest.approx(1.4514, abs=1e-3)
    assert len(chained.matched_events) == 2

    unmatched = explain_share_ratio([ev1], 3.5)
    assert unmatched.explained is False


def test_parsers_cafef_cotuc_rss_and_html_skip_nav() -> None:
    cafef_payload = {
        "Data": [
            {
                "Time": "/Date(1763424000000)/",
                "type": 1,
                "Text": "Trả cổ tức bằng cổ phiếu tỷ lệ 100:20",
            },
            {
                "Time": "/Date(1750000000000)/",
                "type": 2,
                "Text": "Cổ tức bằng tiền năm 2025 tỷ lệ 15%",
            },
        ]
    }
    cafef_events = parse_cafef_corporate_actions(cafef_payload, "FPT")
    assert len(cafef_events) == 2
    assert cafef_events[0].is_share_changing is True
    assert cafef_events[0].share_factor == pytest.approx(1.2)
    assert cafef_events[1].cash_dividend_pct == pytest.approx(15.0)

    cotuc_html = """
    <html><body>
      <a href="/su-kien/2026-11-28/fpt-co-tuc-tien-mat-20-phan-tram">
        <span>FPT</span><span>Cổ tức bằng tiền 20% (2.000đ)</span><span>Thanh toán 15/12/2026</span>
      </a>
      <a href="/su-kien/2026-12-02/vnm-phat-hanh-co-phieu-thuong-10-1">
        <span>VNM</span><span>Thưởng cổ phiếu tỷ lệ 10:1 ngày 05/12/2026</span>
      </a>
    </body></html>
    """
    cal_events = parse_cotuc_calendar_html(cotuc_html, today=date(2026, 11, 18))
    assert len(cal_events) == 2
    by_sym = {e.symbol: e for e in cal_events}
    assert by_sym["FPT"].ex_date == "2026-11-28"
    assert by_sym["FPT"].payment_date == "2026-12-15"
    assert by_sym["FPT"].cash_dividend_pct == pytest.approx(20.0)
    assert by_sym["FPT"].cash_per_share_vnd == pytest.approx(2000.0)
    assert by_sym["FPT"].days_until == 10
    assert by_sym["VNM"].stock_ratio == "10:1"
    assert by_sym["VNM"].share_factor == pytest.approx(1.1)
    assert by_sym["VNM"].date_mismatch_warning is not None

    rss_xml = """<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0"><channel><title>Vietstock</title>
      <item>
        <title>HPG mở rộng công suất Dung Quất 2 trong quý 4</title>
        <link>https://vietstock.vn/hpg-dq2.htm</link>
        <pubDate>Tue, 18 Nov 2026 07:00:00 +0700</pubDate>
        <description>Tập đoàn Hòa Phát đẩy mạnh sản lượng HRC.</description>
      </item>
    </channel></rss>"""
    rss_items = parse_rss_feed_xml(
        rss_xml, source_id="vietstock_corporate", allowed_symbols={"HPG"}
    )
    assert len(rss_items) == 1
    assert rss_items[0].symbols == ["HPG"]
    assert parse_rss_feed_xml("<broken", source_id="vietstock_corporate") == []

    html_doc = """
    <html><body>
      <a href="/login">Đăng nhập</a>
      <a href="/search">⌘K Tìm kiếm nhanh trên trang chứng khoán</a>
      <a href="/img-card"><img src="x.png"/>FPT công bố kết quả kinh doanh tăng trưởng</a>
      <a href="javascript:alert(1)">Thông báo khẩn cổ phiếu FPT cần xem ngay</a>
      <a href="/bai-viet/fpt-tang-truong">
        FPT ghi nhận doanh thu xuất khẩu phần mềm vượt 1 tỷ USD tại Nhật Bản
      </a>
    </body></html>
    """
    links = parse_html_article_links(
        html_doc,
        source_id="vneconomy_equities",
        base_url="https://vneconomy.vn",
    )
    assert len(links) == 1
    assert links[0].url == "https://vneconomy.vn/bai-viet/fpt-tang-truong"
    assert "FPT" in links[0].symbols


@pytest.mark.asyncio
async def test_live_fetch_with_mock_transport_warnings_and_breakers() -> None:
    today_rfc = (datetime.now(UTC) - timedelta(hours=2)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    rss_body = f"""<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>FPT chốt quyền cổ tức tiền mặt</title>
        <link>https://vietstock.vn/fpt-co-tuc.htm</link>
        <pubDate>{today_rfc}</pubDate>
        <description>CTCP FPT thông báo lịch thanh toán cổ tức.</description>
      </item>
    </channel></rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "vietstock.vn/733/rss.rss" in url:
            return httpx.Response(200, text=rss_body)
        if "vneconomy.vn" in url:
            # Non-empty body that yields 0 article links triggers explicit schema-drift warning.
            return httpx.Response(
                200, text="<html><body><div>Empty shell layout</div></body></html>"
            )
        if "cafef.vn/du-lieu/Ajax" in url:
            recent_ms = int((datetime.now(UTC) - timedelta(days=30)).timestamp() * 1000)
            return httpx.Response(
                200,
                json={"Data": [{"Time": f"/Date({recent_ms})/", "Text": "Cổ tức cổ phiếu 10:2"}]},
            )
        if "news.google.com" in url:
            gn_rss = f"""<?xml version="1.0" encoding="utf-8"?>
            <rss version="2.0"><channel>
              <item>
                <title>Công nghệ mở hướng mới trong dự báo, cảnh báo thiên tai</title>
                <link>https://vtv.vn/thoi-tiet-canh-bao-thien-tai.htm</link>
                <pubDate>{today_rfc}</pubDate>
                <description>Cảnh báo lũ quét bằng AI và điện toán đám mây.</description>
              </item>
              <item>
                <title>UBCKNN nhắc nhở FPT chậm công bố thông tin nghị quyết HĐQT</title>
                <link>https://ssc.gov.vn/fpt-notice</link>
                <pubDate>{today_rfc}</pubDate>
                <description>Công bố thông tin chứng khoán trên sàn HOSE đối với FPT.</description>
              </item>
            </channel></rss>"""
            return httpx.Response(200, text=gn_rss)
        if "cotuc.vn" in url:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(503, text="Mocked offline")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as mock_client:
        wire = await fetch_market_news_wire(
            symbols=["FPT"],
            lang="vi",
            breakers=EngineBreakerRegistry(),
            client=mock_client,
        )
        assert wire["partial"] is True
        assert any("layout_changed" in w for w in wire["warnings"])
        assert any(it.title.startswith("FPT") for it in wire["items"])

        events = await fetch_market_events(
            symbols=["FPT"],
            include_listing=False,
            include_insider=False,
            include_regulatory=True,
            breakers=EngineBreakerRegistry(),
            client=mock_client,
        )
        assert events["partial"] is True
        assert any("cotuc_calendar" in s for s in events["sources_failed"])
        titles = [ev.title for ev in events["events"]]
        assert not any("thiên tai" in t for t in titles)
        assert any("FPT" in t and "UBCKNN" in t for t in titles)
        share_evs = [ev for ev in events["events"] if ev.share_factor is not None]
        assert share_evs and share_evs[0].share_factor == pytest.approx(1.2)


def test_rest_endpoints_news_wire_and_events_integration(client) -> None:
    assert static_route_cost("POST", "/v1/market/news-wire") == 3
    assert static_route_cost("POST", "/v1/market/events") is None  # dynamic in _route_cost

    bad_sym = client.post("/v1/market/news-wire", json={"symbols": ["BAD!!SYM"]})
    assert bad_sym.status_code == 400
    assert bad_sym.json()["error"]["code"] == "invalid_request"

    bad_shares = client.post(
        "/v1/market/events",
        json={"symbols": ["FPT"], "observed_shares_high": 1500000.0},
    )
    assert bad_shares.status_code == 400
    assert bad_shares.json()["error"]["code"] == "invalid_request"

    inf_shares = client.post(
        "/v1/market/events",
        json={
            "symbols": ["FPT"],
            "observed_shares_high": 1e15,
            "observed_shares_low": 1e-300,
            "raw_notices": [],
        },
    )
    assert inf_shares.status_code == 400
    assert inf_shares.json()["error"]["code"] == "invalid_request"

    now_iso = datetime.now(UTC).isoformat()
    raw_articles = [
        {
            "title": "FPT thắng thầu dự án chuyển đổi số tại Nhật Bản",
            "url": "https://cafef.vn/fpt-nhat-ban.chn?utm_source=fb",
            "snippet": "CTCP FPT công bố hợp đồng mới trị giá 100 USD triệu đô.",
            "source_id": "cafef_corporate",
            "published_at": now_iso,
        },
        {
            "title": "FPT thắng thầu dự án chuyển đổi số tại Nhật Bản (10:15)",
            "url": "https://vietstock.vn/fpt-nhat-ban.htm",
            "snippet": "Hợp đồng mới kéo dài 5 năm.",
            "source_id": "vietstock_corporate",
            "published_at": now_iso,
        },
        {
            "title": "Tin giả mạo url scheme nguy hiểm cho HPG",
            "url": "javascript:alert(1)",
            "source_id": "untrusted",
            "published_at": now_iso,
        },
    ]

    r1 = client.post(
        "/v1/market/news-wire",
        json={"symbols": ["FPT"], "raw_articles": raw_articles},
    )
    assert r1.status_code == 200
    b1 = r1.json()
    assert b1["total_raw"] == 3
    assert b1["deduplicated_count"] == 1
    assert b1["new_count"] == 1
    assert len(b1["items"]) == 1
    item0 = b1["items"][0]
    assert item0["duplicate_count"] == 2
    assert "USD" not in item0["symbols"]
    assert "FPT" in item0["symbols"]
    assert item0["is_new"] is True

    r2 = client.post(
        "/v1/market/news-wire",
        json={
            "symbols": ["FPT"],
            "raw_articles": raw_articles,
            "seen_fingerprints": b1["new_fingerprints"],
        },
    )
    assert r2.status_code == 200
    assert r2.json()["new_count"] == 0
    assert r2.json()["items"][0]["is_new"] is False

    from datetime import timezone as _tz

    vn_today = datetime.now(_tz(timedelta(hours=7))).date()
    future_ex = (vn_today + timedelta(days=12)).isoformat()
    raw_notices = [
        {
            "event_id": "ev-fpt-div",
            "symbol": "FPT",
            "event_type": "corporate_action",
            "title": "FPT trả cổ tức bằng cổ phiếu tỷ lệ 100:25",
            "event_date": (vn_today - timedelta(days=30)).isoformat(),
            "source_url": "https://hsx.vn/fpt-1",
        },
        {
            "event_id": "ev-fpt-ex",
            "symbol": "FPT",
            "event_type": "dividend_calendar",
            "title": "FPT ngày GDKHQ tạm ứng cổ tức tiền mặt 10%",
            "ex_date": future_ex,
            "cash_dividend_pct": 10.0,
            "source_url": "https://cotuc.vn/su-kien/fpt",
        },
        {
            "event_id": "ev-fpt-warn",
            "symbol": "FPT",
            "event_type": "listing",
            "title": "Cổ phiếu FPT bị đưa vào diện cảnh báo giả định",
            "event_date": vn_today.isoformat(),
        },
    ]

    ev1 = client.post(
        "/v1/market/events",
        json={
            "symbols": ["FPT"],
            "observed_shares_high": 125_000_000.0,
            "observed_shares_low": 100_000_000.0,
            "raw_notices": raw_notices,
        },
    )
    assert ev1.status_code == 200
    eb1 = ev1.json()
    assert len(eb1["events"]) == 3
    assert len(eb1["upcoming_ex_dates"]) == 1
    assert eb1["upcoming_ex_dates"][0]["ex_date"] == future_ex
    assert len(eb1["dilution_alerts"]) >= 1
    assert len(eb1["risk_alerts"]) >= 1
    assert eb1["share_ratio_explanation"]["explained"] is True
    assert eb1["share_ratio_explanation"]["matched_factor"] == pytest.approx(1.25)
    assert eb1["new_events_count"] == 3

    ev2 = client.post(
        "/v1/market/events",
        json={
            "symbols": ["FPT"],
            "include_listing": False,
            "seen_event_ids": ["ev-fpt-div"],
            "raw_notices": raw_notices,
        },
    )
    assert ev2.status_code == 200
    eb2 = ev2.json()
    assert all(e["event_type"] != "listing" for e in eb2["events"])
    assert eb2["new_events_count"] == 1


def test_mcp_client_dispatches_market_news_wire_and_events(monkeypatch: Any) -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def fake_call(
        method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        calls.append((method, path, payload))
        return {"ok": True, "path": path, "symbols": (payload or {}).get("symbols", [])}

    monkeypatch.setattr(mcp_client, "_call", fake_call)

    out_wire = json.loads(
        mcp_client.run_tool("market_news_wire", {"symbols": ["FPT", "VNM"], "max_items": 12})
    )
    out_events = json.loads(
        mcp_client.run_tool(
            "market_events",
            {"symbols": ["FPT"], "reported_share_ratio": 1.25, "lookahead_days": 30},
        )
    )
    assert out_wire["ok"] is True
    assert out_events["ok"] is True
    assert calls[0][0] == "POST" and calls[0][1] == "/v1/market/news-wire"
    assert calls[0][2]["max_items"] == 12
    assert calls[1][0] == "POST" and calls[1][1] == "/v1/market/events"
    assert calls[1][2]["reported_share_ratio"] == pytest.approx(1.25)


def test_round1_ticker_extraction_and_redos_safety() -> None:
    import time as _t

    adversarial = ("1" * 500) + (" " * 1500) + "! FPT công bố kết quả kinh doanh"
    t0 = _t.perf_counter()
    res = extract_tickers_from_text(adversarial)
    elapsed_ms = (_t.perf_counter() - t0) * 1000.0
    assert elapsed_ms < 250.0
    assert "FPT" in res

    assert extract_tickers_from_text("Top 10 cổ phiếu hot nhất và mã vip big pro") == []
    assert extract_tickers_from_text("TIN NÓNG: GIÁ VÀNG VÀ TỶ GIÁ TĂNG MẠNH") == []
    assert extract_tickers_from_text("API: Hướng dẫn tích hợp SDK mới") == []
    assert extract_tickers_from_text(
        "NVDA beats Q4 revenue while BRK.B climbs", allowed_symbols={"NVDA", "BRK.B"}
    ) == [
        "NVDA",
        "BRK.B",
    ]


def test_round1_share_factor_splits_timestamps_and_ratio_dedup() -> None:
    assert parse_share_factor("14:30 - Trả cổ tức bằng cổ phiếu tỷ lệ 100:20") == pytest.approx(1.2)
    assert parse_share_factor("FPT: Thông báo phát hành cổ phiếu ESOP lúc 14:30") is None
    assert parse_share_factor("Tách cổ phiếu tỷ lệ 2:1") == pytest.approx(2.0)
    assert parse_share_factor("NVIDIA 10-for-1 stock split effective") == pytest.approx(10.0)
    assert parse_share_factor("Gộp cổ phiếu tỷ lệ 10:1") == pytest.approx(0.1)
    assert parse_share_factor("Trả cổ tức bằng cổ phiếu tỷ lệ 15%") == pytest.approx(1.15)

    # Mixed cash+stock & stock-%-only CoTuc cards + invalid href date safety
    html_cards = """
    <html><body>
      <a href="/su-kien/2026-02-30/fpt-invalid-feb-30"><span>FPT</span><span>Cổ tức 10%</span></a>
      <a href="/su-kien/2026-12-05/fpt-hon-hop">
        <span>FPT</span><span>Cổ tức bằng tiền 10% (1.500 đồng)</span>
        <span>và thưởng cổ phiếu tỷ lệ 100:20</span>
      </a>
      <a href="/su-kien/2026-12-10/hpg-stock-pct">
        <span>HPG</span><span>Cổ tức bằng cổ phiếu 20%</span>
      </a>
    </body></html>
    """
    cards = parse_cotuc_calendar_html(html_cards, today=date(2026, 11, 18))
    assert len(cards) == 2
    by_sym = {c.symbol: c for c in cards}
    assert by_sym["FPT"].cash_dividend_pct == pytest.approx(10.0)
    assert by_sym["FPT"].cash_per_share_vnd == pytest.approx(1500.0)
    assert by_sym["FPT"].share_factor == pytest.approx(1.2)
    assert by_sym["HPG"].cash_dividend_pct is None
    assert by_sym["HPG"].share_factor == pytest.approx(1.2)

    # explain_share_ratio: no cross-source double count, no cross-symbol mix, best-fit subset
    dup_cafef = CorporateEventRecord(
        event_id="cafef:FPT:2025-06-01:a",
        symbol="FPT",
        exchange="HOSE",
        event_type="corporate_action",
        event_subtype="share_change",
        severity="high",
        risk_level=1,
        title="CafeF FPT 100:23",
        event_date="2025-06-01",
        is_share_changing=True,
        share_factor=1.23,
    )
    dup_cotuc = CorporateEventRecord(
        event_id="cotuc:FPT:2025-06-01:slug",
        symbol="FPT",
        exchange="HOSE",
        event_type="dividend_calendar",
        event_subtype="stock_entitlement",
        severity="medium",
        risk_level=1,
        title="CoTuc FPT 100:23",
        event_date="2025-06-01",
        is_share_changing=True,
        share_factor=1.23,
    )
    fpt_2026 = CorporateEventRecord(
        event_id="cafef:FPT:2026-05-01:b",
        symbol="FPT",
        exchange="HOSE",
        event_type="corporate_action",
        event_subtype="share_change",
        severity="high",
        risk_level=1,
        title="FPT 2026 100:15",
        event_date="2026-05-01",
        is_share_changing=True,
        share_factor=1.15,
    )
    fpt_2024 = CorporateEventRecord(
        event_id="cafef:FPT:2024-06-01:c",
        symbol="FPT",
        exchange="HOSE",
        event_type="corporate_action",
        event_subtype="share_change",
        severity="high",
        risk_level=1,
        title="FPT 2024 100:18",
        event_date="2024-06-01",
        is_share_changing=True,
        share_factor=1.18,
    )
    vnm_2025 = CorporateEventRecord(
        event_id="cafef:VNM:2025-06-01:d",
        symbol="VNM",
        exchange="HOSE",
        event_type="corporate_action",
        event_subtype="share_change",
        severity="high",
        risk_level=1,
        title="VNM 2025 100:20",
        event_date="2025-06-01",
        is_share_changing=True,
        share_factor=1.2,
    )
    # Must NOT double-count 1.23 * 1.23 = 1.5129
    no_double = explain_share_ratio([dup_cafef, dup_cotuc], 1.5129, tolerance=0.02)
    assert no_double.explained is False
    # Must NOT multiply FPT (1.23) * VNM (1.2) = 1.476
    no_cross = explain_share_ratio([dup_cafef, vnm_2025], 1.476, tolerance=0.02)
    assert no_cross.explained is False
    # Must find exact [2025, 2024] = 1.23 * 1.18 = 1.4514 even when 2026 (1.15) comes first
    exact_subset = explain_share_ratio([fpt_2026, dup_cafef, fpt_2024], 1.4514, tolerance=0.08)
    assert exact_subset.explained is True
    assert exact_subset.matched_factor == pytest.approx(1.4514, abs=1e-3)
    assert exact_subset.residual_error_pct == pytest.approx(0.0, abs=0.05)


def test_round1_listing_insider_and_parser_edge_cases() -> None:
    mixed_reinstate = classify_listing_event(
        "HNX: Đưa cổ phiếu ABC ra khỏi diện cảnh báo, nhưng vẫn giữ diện kiểm soát"
    )
    assert mixed_reinstate.subtype == "warning_control"
    assert mixed_reinstate.is_risk_alert is True

    assert classify_listing_event("Quyết định hủy bỏ niêm yết cổ phiếu XYZ").subtype == "delisting"
    assert classify_listing_event("Tạm ngừng giao dịch cổ phiếu KBC").subtype == "warning_control"
    assert classify_listing_event("FPT bổ sung tài liệu ĐHĐCĐ 2026").subtype == "other"

    assert classify_insider_trade("Chào bán cổ phiếu ra công chúng").direction == "unknown"
    assert classify_insider_trade("Tổng Giám đốc bàn chiến lược bán hàng Q4").direction == "unknown"
    cancelled = classify_insider_trade(
        "Chủ tịch đăng ký bán 1.000.000 cp nhưng bất thành, đã bán 0 cp"
    )
    assert cancelled.execution_status == "cancelled"
    assert cancelled.shares_count is None

    atom_xml = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>FPT ký hợp đồng AI Cloud mới</title>
        <link rel="alternate" href="https://example.com/fpt-atom"/>
        <updated>2026-11-18T08:00:00+00:00</updated>
        <summary>Lợi nhuận &amp;lt; 100 tỷ nhưng ROE &amp;gt; 15%.</summary>
      </entry>
    </feed>"""
    atom_items = parse_rss_feed_xml(atom_xml, source_id="atom_feed")
    assert len(atom_items) == 1
    assert atom_items[0].published_at is not None
    assert "< 100 tỷ" in atom_items[0].snippet

    inline_html = (
        '<a href="/bai-viet/nvda">$<b>NVDA</b> đạt tăng trưởng doanh thu <b>15</b>% quý 4</a>'
        '<a href="#top">Lên đầu trang chủ mục tin thị trường ngay</a>'
        '<noscript><a href="/hidden">Nội dung dự phòng dài hơn ba mươi ký tự</a></noscript>'
    )
    links = parse_html_article_links(
        inline_html,
        source_id="test_html",
        base_url="https://example.com",
        allowed_symbols={"NVDA"},
    )
    assert len(links) == 1
    assert links[0].title == "$NVDA đạt tăng trưởng doanh thu 15% quý 4"
    assert links[0].symbols == ["NVDA"]


@pytest.mark.asyncio
async def test_round1_cache_roundtrip_macro_lang_ssrf_redirect_and_mcp_budget(
    monkeypatch: Any,
) -> None:
    from app.cache import CacheClient

    cache = CacheClient()
    today_rfc = (datetime.now(UTC) - timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested_urls.append(url)
        if "vneconomy.vn" in url:
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:9999/internal"})
        if "vietstock.vn/733/rss.rss" in url:
            xml = f"""<?xml version="1.0" encoding="utf-8"?>
            <rss version="2.0"><channel><item>
              <title>FPT mở rộng nhà máy phần mềm mới</title>
              <link>https://vietstock.vn/fpt-nha-may.htm</link>
              <pubDate>{today_rfc}</pubDate>
              <description>Dự án FDI tại Nhật Bản.</description>
            </item></channel></rss>"""
            return httpx.Response(200, text=xml)
        if "vietstock.vn/761/rss.rss" in url:
            xml = f"""<?xml version="1.0" encoding="utf-8"?>
            <rss version="2.0"><channel><item>
              <title>NHNN giữ ổn định lãi suất OMO</title>
              <link>https://vietstock.vn/nhnn-omo.htm</link>
              <pubDate>{today_rfc}</pubDate>
              <description>Diễn biến lãi suất liên ngân hàng.</description>
            </item></channel></rss>"""
            return httpx.Response(200, text=xml)
        return httpx.Response(404, text="not found")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as mock_client:
        # 1. Partial failure (vneconomy SSRF redirect blocked + others 404) must NOT poison cache
        first = await fetch_market_news_wire(
            lang="vi",
            include_market_macro=False,
            breakers=EngineBreakerRegistry(),
            cache=cache,
            client=mock_client,
        )
        assert first["partial"] is True
        assert any("ssrf_redirect_blocked" in s for s in first["sources_failed"])
        assert "127.0.0.1:9999" not in " ".join(requested_urls)
        assert all(it.scope not in ("market", "policy") for it in first["items"])

        # 2. Empty categories=[] must yield [] even if items exist
        empty_cats = await fetch_market_news_wire(
            lang="vi",
            categories=[],
            raw_articles=[
                {"title": "FPT tăng trưởng", "published_at": datetime.now(UTC).isoformat()}
            ],
        )
        assert empty_cats["items"] == []

        # 3. Clean cache roundtrip preserves ISO published_at & date_status="published_at"
        clean_xml = f"""<?xml version="1.0" encoding="utf-8"?>
        <rss version="2.0"><channel><item>
          <title>FPT chốt ngày trả cổ tức tiền mặt</title>
          <link>https://vietstock.vn/fpt-div.htm</link>
          <pubDate>{today_rfc}</pubDate>
        </item></channel></rss>"""

        def all_ok_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=clean_xml)

        async with httpx.AsyncClient(transport=httpx.MockTransport(all_ok_handler)) as ok_client:
            warm1 = await fetch_market_news_wire(
                lang="vi",
                breakers=EngineBreakerRegistry(),
                cache=cache,
                client=ok_client,
            )
            assert warm1["partial"] is False
            assert warm1["items"][0].published_at is not None

            async def boom(_req: httpx.Request) -> httpx.Response:
                raise AssertionError("Should have hit cache")

            async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as fail_if_net:
                warm2 = await fetch_market_news_wire(
                    lang="vi",
                    breakers=EngineBreakerRegistry(),
                    cache=cache,
                    client=fail_if_net,
                )
                assert warm2["items"][0].published_at is not None
                assert warm2["items"][0].date_status == "published_at"

        # 4. lang="en" without fallback must NOT call VN feeds
        requested_urls.clear()
        en_wire = await fetch_market_news_wire(
            lang="en",
            include_search_fallback=False,
            breakers=EngineBreakerRegistry(),
            client=mock_client,
        )
        assert requested_urls == []
        assert en_wire["items"] == []

    # 5. raw_notices respects sym_set filter cleanly
    ev_filtered = await fetch_market_events(
        symbols=["FPT"],
        raw_notices=[
            {"symbol": "HPG", "title": "HPG chia cổ tức 10%"},
            {"symbol": "FPT", "title": "FPT chia cổ tức bằng cổ phiếu 100:20"},
        ],
    )
    assert [e.symbol for e in ev_filtered["events"]] == ["FPT"]

    # 6. MCP client: rejects NaN & enforces _TOTAL_CHAR_BUDGET cleanly
    with pytest.raises(mcp_client.MCPClientError):
        await mcp_client.market_events(reported_share_ratio=float("nan"))

    async def huge_call(method: str, path: str, payload: Any = None) -> dict[str, Any]:
        return {
            "items": [
                {"title": f"Item {i}", "snippet": "X" * 400, "stable_id": f"id-{i}"}
                for i in range(60)
            ]
        }

    monkeypatch.setattr(mcp_client, "_call", huge_call)
    bounded_raw = await mcp_client.market_news_wire(symbols=["FPT"])
    assert len(bounded_raw) <= 12_000
    parsed_mcp = json.loads(bounded_raw)
    assert parsed_mcp["truncated"] is True


def test_round2_parsers_security_math_and_entities() -> None:
    import time

    from app.api.v1.schemas import _sanitize_http_url
    from app.retrieval.events import _parse_locale_number, _safe_http_url, _strip_tags

    # 1. Zero-ReDoS _strip_tags + preserves <EPS and P/E> + strips <script>/<style>
    t0 = time.perf_counter()
    res_redos = _strip_tags("<a" + " " * 2500)
    assert (time.perf_counter() - t0) < 0.1
    assert isinstance(res_redos, str)

    cleaned_tag = _strip_tags(
        "<b>Hello</b> <script>alert(1)</script><style>.x{}</style> &lt;EPS and P/E&gt; <Cty ABC>"
    )
    assert "alert(1)" not in cleaned_tag
    assert "<EPS and P/E>" in cleaned_tag
    assert "<Cty ABC>" in cleaned_tag

    # 2. _safe_http_url & _sanitize_http_url reject @ in netloc & bad ports
    for bad_u in (
        "https://@evil.com/path",
        "https://user@evil.com/path",
        "https://example.com:99999/ok",
        "ftp://example.com/a",
    ):
        assert _safe_http_url(bad_u) is None
        assert _sanitize_http_url(bad_u) is None
    assert _safe_http_url("https://cafef.vn/news?id=1") == "https://cafef.vn/news?id=1"

    # 3. Decimal vs thousands parsing
    assert _parse_locale_number("0.250") == pytest.approx(0.25)
    assert _parse_locale_number("0,250") == pytest.approx(0.25)
    assert _parse_locale_number("1.500") == pytest.approx(1500.0)

    # 4. Entity extraction: VN30 BID/GAS/POW, F88/L14, exchange prefix, commodity/crypto guards
    assert "BID" in extract_tickers_from_text("VCB, BID, CTG dẫn dắt VN-Index tăng mạnh")
    assert "GAS" in extract_tickers_from_text("HPG, GAS, FPT đóng góp tích cực vào đà tăng")
    assert "POW" in extract_tickers_from_text("VCB, POW, SSI giao dịch khởi sắc")
    assert "BID" in extract_tickers_from_text("Nhận định (HOSE: BID) và (HSX: GAS)")
    assert "F88" in extract_tickers_from_text("Cổ phiếu F88 và L14 biến động mạnh")
    assert "GAS" not in extract_tickers_from_text("Natural GAS prices jump at Henry Hub LNG")
    assert "POW" not in extract_tickers_from_text("Bitcoin BTC POW mining hashrate hits high")

    # 5. Share-factor math, clock timestamps, prose colons & negation guards
    assert parse_share_factor(
        "08:30 ngày 18/11 FPT phát hành cổ tức bằng cổ phiếu 10:1"
    ) == pytest.approx(1.1)
    assert (
        parse_share_factor("09:15 FPT họp (thời gian 14:30 ngày 18/11) chia cổ tức tiền mặt")
        is None
    )
    assert parse_share_factor("Chia tách 1:5 có hiệu lực từ ngày mai") == pytest.approx(5.0)
    assert parse_share_factor("Gộp 10 cổ phiếu thành 1 cho cổ đông hiện hữu") == pytest.approx(0.1)
    assert parse_share_factor("Tách 1 cổ phiếu thành 5 cổ phiếu trong quý 4") == pytest.approx(5.0)
    assert parse_share_factor("Cổ tức năm 2025: 15% bằng cổ phiếu") == pytest.approx(1.15)
    assert parse_share_factor("Trả 15% cổ tức bằng cổ phiếu") == pytest.approx(1.15)
    assert parse_share_factor("Cổ tức bằng tiền và cổ phiếu: 20%") == pytest.approx(1.2)
    assert parse_share_factor("Phát hành đợt 1: 100.000 cp") is None

    # Negated reinstatement must stay risky
    neg_list = classify_listing_event(
        "Cổ phiếu bị giữ diện kiểm soát vì chưa khắc phục để ra khỏi diện"
    )
    assert neg_list.subtype != "reinstated"
    neg_reg = classify_regulatory_action("Xử phạt nếu chưa khắc phục xong tình trạng cảnh báo")
    assert neg_reg.subtype != "reinstated" and neg_reg.risk_level >= 1

    # 6. Atom XHTML + <guid> + _AnchorExtractor inline span & indentation & nested <a name>
    atom_xml = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>$NVDA beats estimates cleanly</title>
        <link rel="alternate" href="https://example.com/nvda"/>
        <summary type="xhtml">
          <div xmlns="http://www.w3.org/1999/xhtml"><p>AI demand</p><p>grows</p></div>
        </summary>
        <updated>2026-11-18T08:00:00Z</updated>
      </entry>
    </feed>"""
    atom_items = parse_rss_feed_xml(atom_xml, source_id="atom_src", today=date(2026, 11, 18))
    assert len(atom_items) == 1
    assert "NVDA" in atom_items[0].symbols
    assert "AI demand" in atom_items[0].snippet and "grows" in atom_items[0].snippet

    rss_guid_xml = """<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0"><channel><item>
      <title>HPG mở rộng sản xuất thép HRC</title>
      <guid isPermaLink="true">https://vietstock.vn/hpg-hrc</guid>
      <pubDate>Wed, 18 Nov 2026 07:00:00 +0700</pubDate>
    </item></channel></rss>"""
    assert parse_rss_feed_xml(rss_guid_xml, source_id="vs")[0].url == "https://vietstock.vn/hpg-hrc"

    ws = " " * 600
    html_links = f"""<html><body>
      <a href="/nvda">{ws}<a name="top"></a>$<span>NVDA</span> surges on strong Q3 results</a>
    </body></html>"""
    parsed_links = parse_html_article_links(
        html_links, source_id="wire_html", base_url="https://example.com", min_title_len=20
    )
    assert len(parsed_links) == 1
    assert "$NVDA surges" in parsed_links[0].title
    assert "NVDA" in parsed_links[0].symbols

    # 7. CoTuc par/offering price guard + mixed cash+stock + BID/GAS + CafeF clause isolation
    cotuc_edge = """<html><body>
      <a href="/su-kien/2026-12-01/bid-phat-hanh-them-co-phieu">
        <span>BID</span>
        <span>Phát hành cổ phiếu mệnh giá 10.000 đồng, giá chào bán 15.000 đồng/cp</span>
      </a>
      <a href="/su-kien/2026-12-05/gas-co-tuc-tien-va-thuong">
        <span>GAS</span><span>Cổ tức bằng tiền 500đ/cp và thưởng cổ phiếu 20%</span>
      </a>
    </body></html>"""
    c_evs = {e.symbol: e for e in parse_cotuc_calendar_html(cotuc_edge, today=date(2026, 11, 18))}
    assert "BID" in c_evs
    assert c_evs["BID"].cash_per_share_vnd is None
    assert c_evs["BID"].event_subtype == "stock_entitlement"
    assert c_evs["GAS"].cash_per_share_vnd == pytest.approx(500.0)
    assert c_evs["GAS"].cash_dividend_pct is None
    assert c_evs["GAS"].share_factor == pytest.approx(1.2)
    assert c_evs["GAS"].event_subtype == "mixed_dividend"

    cafef_mixed = parse_cafef_corporate_actions(
        {
            "Data": [
                {
                    "Time": "/Date(1763424000000)/",
                    "Text": "Tăng vốn điều lệ 30% và trả cổ tức bằng tiền 1.000đ/cp",
                },
                {
                    "Time": "/Date(1763337600000)/",
                    "Text": "Chi trả cổ tức bằng tiền mặt 1.500 đồng (15%)",
                },
            ]
        },
        "FPT",
    )
    assert cafef_mixed[0].cash_per_share_vnd == pytest.approx(1000.0)
    assert cafef_mixed[0].cash_dividend_pct is None
    assert cafef_mixed[1].cash_per_share_vnd == pytest.approx(1500.0)
    assert cafef_mixed[1].cash_dividend_pct == pytest.approx(15.0)

    # 8. explain_share_ratio: preserve observed_ratio, 10-day dedup, no unattributed chain
    t_cafef = CorporateEventRecord(
        event_id="cf1",
        symbol="FPT",
        exchange="HOSE",
        event_type="corporate_action",
        event_subtype="share_change",
        severity="medium",
        risk_level=1,
        title="FPT 100:20",
        event_date="2026-06-10",
        share_factor=1.2,
        is_share_changing=True,
    )
    t_cotuc = CorporateEventRecord(
        event_id="ct1",
        symbol="FPT",
        exchange="HOSE",
        event_type="dividend_calendar",
        event_subtype="stock_entitlement",
        severity="medium",
        risk_level=1,
        title="FPT 100:20 GDKHQ",
        event_date="2026-06-11",
        share_factor=1.2,
        is_share_changing=True,
    )
    dedup_ratio = explain_share_ratio([t_cafef, t_cotuc], 1.44, tolerance=0.02)
    assert dedup_ratio.explained is False  # same action T vs T+1 must NOT chain into 1.44

    inv_res = explain_share_ratio([t_cafef], 1.0 / 1.2, tolerance=0.02)
    assert inv_res.explained is True
    assert inv_res.observed_ratio == pytest.approx(round(1.0 / 1.2, 4))

    anon1 = CorporateEventRecord(
        event_id="a1",
        symbol=None,
        exchange=None,
        event_type="corporate_action",
        event_subtype="share_change",
        severity="medium",
        risk_level=1,
        title="Anon 2:1",
        event_date="2026-01-01",
        share_factor=2.0,
        is_share_changing=True,
    )
    anon2 = CorporateEventRecord(
        event_id="a2",
        symbol="",
        exchange=None,
        event_type="corporate_action",
        event_subtype="share_change",
        severity="medium",
        risk_level=1,
        title="Anon2 100:20",
        event_date="2026-03-01",
        share_factor=1.2,
        is_share_changing=True,
    )
    assert explain_share_ratio([anon1, anon2], 2.4, tolerance=0.02).explained is False

    old_undated = CorporateEventRecord(
        event_id="u0",
        symbol="VNM",
        exchange="HOSE",
        event_type="corporate_action",
        event_subtype="share_change",
        severity="medium",
        risk_level=1,
        title="Undated noise",
        event_date=None,
        share_factor=9.0,
        is_share_changing=True,
    )
    recent_evs = [
        CorporateEventRecord(
            event_id=f"r{i}",
            symbol="VNM",
            exchange="HOSE",
            event_type="corporate_action",
            event_subtype="share_change",
            severity="medium",
            risk_level=1,
            title=f"Action {i}",
            event_date=f"2026-0{i + 1}-15",
            share_factor=1.5 if i == 0 else 1.01,
            is_share_changing=True,
        )
        for i in range(8)
    ]
    assert explain_share_ratio([old_undated, *recent_evs], 1.5, tolerance=0.02).explained is True

    # 9. dedup policy promotion, general_finance upgrade, date_status preserve & pre-1970 sort
    p1 = ParsedNewsItem(
        title="NHNN điều hành tỷ giá và lãi suất đối với VCB",
        url=None,
        source_id="vs1",
        snippet="",
        published_at=None,
        event_date="2026-12-01",
        symbols=[],
        scope="policy",
        category="general_finance",
        severity="info",
        stable_id="s1",
        fingerprint="fp-policy-1",
        date_status="event_date",
    )
    p2 = ParsedNewsItem(
        title="NHNN điều hành tỷ giá và lãi suất đối với VCB",
        url=None,
        source_id="vs2",
        snippet="Chi tiết chính sách cổ tức",
        published_at=datetime(2026, 11, 18, 9, 0, tzinfo=UTC),
        event_date=None,
        symbols=["VCB"],
        scope="ticker",
        category="corporate_action",
        severity="high",
        stable_id="s2",
        fingerprint="fp-policy-1",
        date_status="published_at",
    )
    merged = deduplicate_news_items([p1, p2])
    assert len(merged) == 1
    assert merged[0].scope == "ticker"
    assert merged[0].category == "corporate_action"
    assert merged[0].severity == "high"
    assert merged[0].date_status == "event_date"

    # 10. MCP strict JSON budget with huge auxiliary lists & no input mutation
    huge_payload = {
        "events": [
            {
                "event_id": f"e{i}",
                "title": "X" * 300,
                "detail": "Y" * 500,
                "also_reported_by": [f"src_{j}" for j in range(100)],
                "symbols": ["FPT", "VNM"],
            }
            for i in range(5)
        ],
        "warnings": ["W" * 500 for _ in range(30)],
        "share_ratio_explanation": {
            "explained": True,
            "explanation": "E" * 2000,
            "matched_events": [{"a": "B" * 2000}],
        },
    }
    snapshot = json.dumps(huge_payload, sort_keys=True)
    dumped = mcp_client._bounded_json_dumps(huge_payload, ("events",))
    assert len(dumped) <= 12_000
    assert json.loads(dumped)["truncated"] is True
    assert json.dumps(huge_payload, sort_keys=True) == snapshot


def test_round2_mcp_loop_affinity_and_auth_504(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    import app.api.v1.market as market_mod
    import app.core.auth as auth_mod

    # Sequential asyncio.run() loops recycle _SHARED_CLIENT cleanly without "Event loop is closed"
    monkeypatch.setattr(mcp_client, "_SHARED_CLIENT", None)
    monkeypatch.setattr(mcp_client, "_SHARED_CLIENT_LOOP", None)
    clients_created: list[Any] = []

    class _FakeRespClient:
        is_closed = False

        def __init__(self, loop_ref: Any) -> None:
            self.loop_ref = loop_ref
            clients_created.append(self)

        async def request(self, method: str, url: str, **_kw: Any) -> httpx.Response:
            cur = asyncio.get_running_loop()
            if self.loop_ref is not cur:
                raise RuntimeError("Event loop is closed")
            return httpx.Response(200, json={"items": [], "new_count": 0})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **_kw: _FakeRespClient(asyncio.get_running_loop()),
    )
    out1 = mcp_client.run_tool("market_news_wire", {"symbols": ["FPT"]})
    out2 = mcp_client.run_tool("market_news_wire", {"symbols": ["VNM"]})
    assert len(clients_created) == 2
    assert json.loads(out1)["new_count"] == 0
    assert json.loads(out2)["new_count"] == 0

    # Auth check runs BEFORE _route_cost JSON body parsing
    from app.config import get_settings

    called = {"cost": False}

    async def _fail_if_cost(_req: Any) -> int:
        called["cost"] = True
        return 3

    async def _deny_auth(_bearer: str) -> Any:
        from app.core.errors import ErrorCode, MetaSearchError

        raise MetaSearchError(ErrorCode.UNAUTHORIZED, "Invalid API key", status_code=401)

    with monkeypatch.context() as auth_patch:
        auth_patch.setenv("AUTH_ENABLED", "true")
        get_settings.cache_clear()
        try:
            auth_patch.setattr(auth_mod, "_route_cost", _fail_if_cost)
            auth_patch.setattr(auth_mod, "authenticate", _deny_auth)
            r401 = client.post(
                "/v1/market/events",
                json={"symbols": ["FPT"]},
                headers={"Authorization": "Bearer bad"},
            )
            assert r401.status_code == 401
            assert called["cost"] is False
        finally:
            get_settings.cache_clear()

    # 504 on budget exhaustion must emit deadline_exceeded

    class _ExhaustedBudget:
        def remaining(self) -> float:
            return 0.0

    monkeypatch.setattr(
        market_mod, "create_budget_for_route", lambda *_a, **_kw: _ExhaustedBudget()
    )
    r504_wire = client.post("/v1/market/news-wire", json={"symbols": ["FPT"]})
    assert r504_wire.status_code == 504
    assert r504_wire.json()["error"]["code"] == "deadline_exceeded"
    r504_ev = client.post("/v1/market/events", json={"symbols": ["FPT"]})
    assert r504_ev.status_code == 504
    assert r504_ev.json()["error"]["code"] == "deadline_exceeded"


@pytest.mark.asyncio
async def test_round2_async_orchestrator_pinning_partial_timeout_and_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from app.extraction import pinning, ssrf
    from app.retrieval.events import _fetch_source_text

    # 1. raw_notices stock_ratio is NOT squared into 1.21 / 0.01 / 4.0
    ev_raw = await fetch_market_events(
        symbols=["FPT", "VNM", "HPG"],
        raw_notices=[
            {"symbol": "FPT", "title": "FPT chia cổ tức 10:1", "stock_ratio": "10:1"},
            {"symbol": "VNM", "title": "Gộp cổ phiếu 10:1", "stock_ratio": "10:1"},
            {"symbol": "HPG", "title": "Tách cổ phiếu 2:1", "stock_ratio": "2:1"},
        ],
    )
    by_s = {e.symbol: e for e in ev_raw["events"]}
    assert by_s["FPT"].share_factor == pytest.approx(1.1)
    assert by_s["VNM"].share_factor == pytest.approx(0.1)
    assert by_s["HPG"].share_factor == pytest.approx(2.0)

    # 2. _fetch_source_text SSRF IP pinning, malformed URL ValueError, deferred Outcome.OK
    seen_req: dict[str, Any] = {}

    class _PinPool:
        def stream(
            self,
            method: str,
            url: str,
            *,
            headers: dict[str, str],
            extensions: dict[str, Any] | None = None,
            **_kw: Any,
        ):
            seen_req["url"] = url
            seen_req["headers"] = dict(headers)
            seen_req["extensions"] = dict(extensions or {})

            class _RespCtx:
                status_code = 200
                encoding = "utf-8"
                charset_encoding = "utf-8"
                headers: dict[str, str] = {}

                async def __aenter__(self) -> Any:
                    return self

                async def __aexit__(self, *_a: Any) -> bool:
                    return False

                async def aiter_bytes(self):
                    yield b"<html></html>"

            return _RespCtx()

    async def _fake_resolve(url: str) -> frozenset[str]:
        if "[" in url:
            raise ValueError("Invalid IPv6 URL")
        return frozenset({"93.184.216.34"})

    monkeypatch.setattr(ssrf, "resolve_and_check", _fake_resolve)
    monkeypatch.setattr("app.retrieval.events.get_http_client", lambda *_a: _PinPool())
    reg = EngineBreakerRegistry(threshold=3)
    txt, err = await _fetch_source_text(
        "test_pin",
        "https://vietstock.vn/feed.rss",
        timeout_s=2.0,
        breakers=reg,
        client=None,
        defer_ok=True,
    )
    assert err is None and txt == "<html></html>"
    assert "93.184.216.34" in seen_req["url"]
    assert seen_req["headers"]["Host"] == "vietstock.vn"
    assert seen_req["headers"]["Accept-Encoding"] == "identity"
    assert seen_req["extensions"].get("sni_hostname") == "vietstock.vn"

    bad_txt, bad_err = await _fetch_source_text(
        "bad_url", "https://[::invalid", timeout_s=2.0, breakers=reg, client=None
    )
    assert bad_txt is None and "ssrf_blocked" in (bad_err or "")

    # 3. Partial timeout via asyncio.wait salvages fast RSS feed + isolates _one_cotuc crash
    today_rfc = (datetime.now(UTC) - timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    fast_rss = f"""<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0"><channel><item>
      <title>FPT chi trả cổ tức năm 2026</title>
      <link>https://vietstock.vn/fpt-fast</link>
      <pubDate>{today_rfc}</pubDate>
    </item></channel></rss>"""

    async def _handler_partial(request: httpx.Request) -> httpx.Response:
        u = str(request.url)
        if "vietstock.vn/733/rss.rss" in u:
            return httpx.Response(200, text=fast_rss)
        if "cotuc.vn" in u:
            raise RuntimeError("boom cotuc")
        await asyncio.sleep(2.5)
        return httpx.Response(200, text=fast_rss)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler_partial)) as pclient:
        wire_partial = await fetch_market_news_wire(
            symbols=["FPT"],
            lang="vi",
            timeout_s=0.45,
            breakers=EngineBreakerRegistry(),
            client=pclient,
        )
        assert wire_partial["partial"] is True
        assert any("FPT" in it.title for it in wire_partial["items"])
        assert any("timeout" in s for s in wire_partial["sources_failed"])

        ev_cotuc_boom = await fetch_market_events(
            symbols=["FPT"],
            include_corporate_actions=False,
            include_listing=False,
            include_insider=False,
            include_regulatory=False,
            timeout_s=1.0,
            breakers=EngineBreakerRegistry(),
            client=pclient,
        )
        assert ev_cotuc_boom["partial"] is True
        assert any("cotuc_calendar" in s for s in ev_cotuc_boom["sources_failed"])

    # 4. Cache corruption resilience + midnight days_until refresh + pre-1970 sort stability
    yesterday_str = (datetime.now(UTC) + timedelta(days=5)).date().isoformat()

    class _MixedCache:
        async def get_json(self, key: str) -> Any:
            if key.startswith("cotuc_calendar"):
                return {
                    "events": [
                        {"event_id": "bad", "title": 123},  # malformed types -> dropped
                        {
                            "event_id": "ok1",
                            "symbol": "FPT",
                            "event_type": "dividend_calendar",
                            "event_subtype": "cash_dividend",
                            "severity": "medium",
                            "risk_level": 0,
                            "title": "FPT ex-date cached",
                            "event_date": yesterday_str,
                            "ex_date": yesterday_str,
                            "days_until": 999,  # stale -> must be recomputed
                        },
                    ]
                }
            if key.startswith("news_wire"):
                raise RuntimeError("redis corrupted")
            return None

        async def set_json(self, *_a: Any, **_kw: Any) -> None:
            return None

    async def _all_503(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="err")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_all_503)) as c503:
        # News wire survives cache exception gracefully (fails over to feeds)
        res_corrupt_wire = await fetch_market_news_wire(
            lang="vi", cache=_MixedCache(), breakers=EngineBreakerRegistry(), client=c503
        )
        assert res_corrupt_wire["partial"] is True

        # Cached CoTuc drops invalid entry and refreshes days_until from 999 -> <= 6
        res_ev_cache = await fetch_market_events(
            symbols=["FPT"],
            include_corporate_actions=False,
            include_listing=False,
            include_insider=False,
            include_regulatory=False,
            cache=_MixedCache(),
            breakers=EngineBreakerRegistry(),
            client=c503,
        )
        assert len(res_ev_cache["events"]) == 1
        assert res_ev_cache["events"][0].days_until != 999
        assert 0 <= (res_ev_cache["events"][0].days_until or 0) <= 6

    # Pre-1970 published_at sort stability on Windows
    # Keep the 1965 item via future event_date so sort compares both negative/positive epochs
    future_ev_d = (datetime.now(UTC) + timedelta(days=3)).date().isoformat()
    pre1970 = await fetch_market_news_wire(
        max_age_days=90,
        raw_articles=[
            {
                "title": "FPT old epoch news",
                "published_at": "1965-05-01T00:00:00Z",
                "event_date": future_ev_d,
            },
            {"title": "FPT fresh news", "published_at": datetime.now(UTC).isoformat()},
        ],
    )
    assert [it.title for it in pre1970["items"]] == ["FPT fresh news", "FPT old epoch news"]
    _ = pinning


def test_round3_finance_precision_clauses_and_negations() -> None:
    from app.retrieval.events import (
        _extract_cash_dividend,
        _extract_cotuc_ticker,
        _extract_ratio_tokens,
        _parse_locale_number,
    )
    from app.retrieval.vn_tickers import get_vn_ticker_symbols

    # 1. "đồng/cổ phiếu" must not trip stock guards; mixed clauses must stay isolated
    cotuc_cash = parse_cotuc_calendar_html(
        '<a href="/su-kien/2026-12-10/fpt-chi-tra-co-tuc">'
        "<span>FPT</span><span>Chi trả cổ tức năm 2025: 1.500 đồng/cổ phiếu</span></a>",
        today=date(2026, 11, 18),
    )
    assert len(cotuc_cash) == 1
    assert cotuc_cash[0].event_subtype == "cash_dividend"
    assert cotuc_cash[0].cash_per_share_vnd == pytest.approx(1500.0)
    assert cotuc_cash[0].risk_level == 0

    cafef_cash = parse_cafef_corporate_actions(
        {
            "Data": [
                {
                    "Time": "/Date(1763424000000)/",
                    "Text": "Chi trả cổ tức năm 2025: 1.500 đồng/cổ phiếu",
                }
            ]
        },
        "FPT",
    )
    assert cafef_cash[0].event_subtype == "cash_dividend"
    assert cafef_cash[0].cash_per_share_vnd == pytest.approx(1500.0)
    assert _extract_cash_dividend(
        "Tạm ứng cổ tức 15% (1.500 đồng/cổ phiếu)", has_stock=True
    ) == pytest.approx((15.0, 1500.0))

    mixed_cases = [
        ("Cổ tức năm 2025: 15% tiền mặt, 10% cổ phiếu", 15.0, 1.1),
        ("Cổ tức bằng tiền và cổ phiếu: 15% tiền mặt, 20% cổ phiếu", 15.0, 1.2),
        ("Cổ tức bằng tiền mặt kèm cổ phiếu thưởng 20%", None, 1.2),
        ("Chi trả cổ tức bằng cổ phiếu đợt 2 và cổ tức tiền mặt 15%", 15.0, None),
        ("Cổ tức 10% bằng tiền mặt, cổ phiếu thưởng 20%", 10.0, 1.2),
        ("Cổ tức 10% tiền mặt / 20% cổ phiếu", 10.0, 1.2),
    ]
    for txt, exp_cash, exp_factor in mixed_cases:
        c_pct, _ = _extract_cash_dividend(txt, has_stock=exp_factor is not None)
        s_fac = parse_share_factor(txt)
        if exp_cash is None:
            assert c_pct is None, txt
        else:
            assert c_pct == pytest.approx(exp_cash), txt
        if exp_factor is None:
            assert s_fac is None, txt
        else:
            assert s_fac == pytest.approx(exp_factor), txt

    # 2. Reverse/forward splits, 1-for-10, unanchored tách, clock timestamps & ratio range
    assert parse_share_factor("Thực hiện gộp tỷ lệ 10:1 cổ phiếu") == pytest.approx(0.1)
    assert parse_share_factor("Gộp cổ phần tỷ lệ 10:1") == pytest.approx(0.1)
    assert parse_share_factor("1-for-10 stock split") == pytest.approx(0.1)
    assert parse_share_factor(
        "Phát hành cổ phiếu thưởng tỷ lệ 10:2 (tách riêng phần cổ phiếu lẻ)"
    ) == pytest.approx(1.2)
    assert parse_share_factor(
        "Chốt quyền tại 14:30, chia cổ tức cổ phiếu tỷ lệ 100:20"
    ) == pytest.approx(1.2)
    assert parse_share_factor(
        "18/11/2026 15:30 - Trả cổ tức bằng cổ phiếu tỷ lệ 100:20"
    ) == pytest.approx(1.2)
    assert _extract_ratio_tokens("Nâng tỷ lệ cổ tức bằng cổ phiếu từ 10:15 lên 10:20") == [
        "10:15",
        "10:20",
    ]

    # 3. Par/offering/tax leaks vs tương đương payout & 4-digit decimal
    assert _extract_cash_dividend(
        "Cổ tức bằng tiền 10% (mệnh giá là 10.000 đồng)", has_stock=False
    ) == (10.0, None)
    assert _extract_cash_dividend(
        "Cổ tức bằng tiền 10%, giá phát hành cổ phiếu ESOP 10.000 đồng/cp", has_stock=False
    ) == (10.0, None)
    assert _extract_cash_dividend("Cash dividend 10% (par value 10,000 VND)", has_stock=False) == (
        10.0,
        None,
    )
    assert _extract_cash_dividend(
        "Chi trả cổ tức bằng tiền tỷ lệ 10%, tương đương 1.000 đồng/cp", has_stock=False
    ) == pytest.approx((10.0, 1000.0))
    assert _extract_cash_dividend(
        "Cổ tức bằng tiền mặt (đã khấu trừ thuế TNCN 5%)", has_stock=False
    ) == (None, None)
    assert _parse_locale_number("1234.567") == pytest.approx(1234.567)

    # 4. Exchange-qualified, CoTuc VND currency guard, BID/OIL&GAS/G20
    assert extract_tickers_from_text("HOSE: 10 cổ phiếu tăng trần") == []
    assert extract_tickers_from_text("HOSE: VN30 tăng mạnh cuối phiên") == []
    assert extract_tickers_from_text("HOSE: BCTC quý 3 năm 2026") == []
    assert extract_tickers_from_text("NASDAQ: 100 stocks rally") == []
    assert extract_tickers_from_text("Hội nghị G20 bàn về kinh tế") == []
    assert extract_tickers_from_text("BID-ASK spread widens on stock exchange") == []
    assert extract_tickers_from_text("APPLE STOCK JUMPS ON ACQUISITION BID") == []
    assert extract_tickers_from_text("OIL & GAS STOCKS SURGE TODAY") == []
    assert (
        _extract_cotuc_ticker("fpt-co-tuc", ["1.500", "VND", "cổ tức"], get_vn_ticker_symbols())
        == "FPT"
    )

    # 5. Listing/regulatory negation & delisting priority & cancel-warning
    assert (
        classify_listing_event("HOSE: Không đưa cổ phiếu ABC ra khỏi diện cảnh báo").subtype
        != "reinstated"
    )
    assert (
        classify_listing_event("HNX: Từ chối đưa cổ phiếu ABC ra khỏi diện kiểm soát").subtype
        != "reinstated"
    )
    assert (
        classify_listing_event(
            "Cổ phiếu ABC chưa được giao dịch trở lại và tiếp tục bị đình chỉ"
        ).subtype
        != "reinstated"
    )
    assert (
        classify_listing_event(
            "Cổ phiếu ABC từng được đưa ra khỏi diện cảnh báo nay bị hủy niêm yết"
        ).subtype
        == "delisting"
    )
    reg_cancel = classify_regulatory_action(
        "HOSE: Quyết định hủy bỏ cảnh báo, đưa cổ phiếu XYZ ra khỏi diện cảnh báo"
    )
    assert reg_cancel.subtype != "sanction" and reg_cancel.risk_level == 0

    # 6. explain_share_ratio non-ISO "TBD" dedup + 3-item dedup alias
    ev_tbd = CorporateEventRecord(
        event_id="tbd",
        symbol="FPT",
        exchange="HOSE",
        event_type="corporate_action",
        event_subtype="share_change",
        severity="medium",
        risk_level=1,
        title="FPT TBD",
        event_date="TBD",
        share_factor=1.2,
        is_share_changing=True,
    )
    ev_iso = CorporateEventRecord(
        event_id="iso",
        symbol="FPT",
        exchange="HOSE",
        event_type="corporate_action",
        event_subtype="share_change",
        severity="medium",
        risk_level=1,
        title="FPT 2026",
        event_date="2026-06-01",
        share_factor=1.2,
        is_share_changing=True,
    )
    assert explain_share_ratio([ev_tbd, ev_iso], 1.44, tolerance=0.02).explained is False

    fp3 = content_fingerprint("Công bố báo cáo tài chính kiểm toán năm 2026")
    d3 = deduplicate_news_items(
        [
            ParsedNewsItem(
                title="Công bố báo cáo tài chính kiểm toán năm 2026",
                url=None,
                source_id="s1",
                symbols=[],
                fingerprint=fp3,
            ),
            ParsedNewsItem(
                title="Công bố báo cáo tài chính kiểm toán năm 2026",
                url="https://a.vn/1",
                source_id="s2",
                symbols=["FPT"],
                fingerprint=fp3,
            ),
            ParsedNewsItem(
                title="Công bố báo cáo tài chính kiểm toán năm 2026",
                url="https://b.vn/2",
                source_id="s3",
                symbols=["FPT"],
                fingerprint=fp3,
            ),
        ]
    )
    assert len(d3) == 1 and d3[0].duplicate_count == 3


@pytest.mark.asyncio
async def test_round3_security_gzip_bomb_overflow_cache_xml_and_cancel(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import zlib

    from app.retrieval.events import (
        _build_raw_notice_events,
        _deserialize_event_records,
        _fetch_source_text,
        _strip_tags,
    )

    # 1. _build_raw_notice_events "1 thành 5" + text cash dividend fallback
    raw_evs = _build_raw_notice_events(
        [
            {"symbol": "FPT", "title": "FPT tách cổ phiếu", "stock_ratio": "1 thành 5"},
            {"symbol": "VNM", "title": "VNM chi trả cổ tức bằng tiền mặt 1.500 đồng/cổ phiếu"},
        ],
        {"FPT", "VNM"},
    )
    assert raw_evs[0].share_factor == pytest.approx(5.0)
    assert raw_evs[1].cash_per_share_vnd == pytest.approx(1500.0)

    # 2. _TAG_OR_COMMENT_RE long <a href=">400ch"> stripped + iterative _element_text depth limit
    long_url = "https://news.google.com/rss/articles/" + ("x" * 500)
    assert "<a" not in _strip_tags(f'<a href="{long_url}">Đọc bài</a> chi tiết')
    deep_xml = (
        "<feed xmlns='http://www.w3.org/2005/Atom'><entry><title>NVDA</title><summary type='xhtml'>"
        + ("<div>" * 1200)
        + "$NVDA"
        + ("</div>" * 1200)
        + "</summary></entry></feed>"
    )
    deep_items = parse_rss_feed_xml(deep_xml, source_id="atom", today=date(2026, 11, 18))
    assert len(deep_items) == 1

    # 3. DefusedXmlException caught cleanly & HTML urljoin malformed link skipped
    assert parse_rss_feed_xml('<?xml version="1.0"?><!DOCTYPE foo><rss/>', source_id="x") == []
    links_bad = parse_html_article_links(
        '<html><body><a href="http://[::invalid">Invalid link title long enough</a>'
        '<a href="https://cafef.vn/ok">Valid FPT article headline long enough</a></body></html>',
        source_id="cafef",
        base_url="https://cafef.vn",
    )
    assert len(links_bad) == 1

    # 4. Bool reported_share_ratio rejected by MCP client
    with pytest.raises(mcp_client.MCPClientError):
        await mcp_client.market_events(reported_share_ratio=True)  # type: ignore[arg-type]

    # 5. Streaming decompression bomb blocked at chunk level before full expansion
    bomb_bytes = zlib.compress(b"A" * (6 * 1024 * 1024), wbits=16 + zlib.MAX_WBITS)

    class _BombStream(httpx.AsyncByteStream):
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        async def __aiter__(self):
            yield self._payload

    def bomb_handler(request: httpx.Request) -> httpx.Response:
        if "bomb" in str(request.url):
            return httpx.Response(
                200,
                stream=_BombStream(bomb_bytes),
                headers={"Content-Encoding": "gzip"},
            )
        if "js_redir" in str(request.url):
            return httpx.Response(302, headers={"Location": "javascript:alert(1)"})
        if "ssrf_redir" in str(request.url):
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/secret"})
        return httpx.Response(302, headers={"Location": "http://[::invalid"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(bomb_handler)) as bclient:
        reg = EngineBreakerRegistry(threshold=3)
        res_b, err_b = await _fetch_source_text(
            "bomb", "https://example.com/bomb", timeout_s=2.0, breakers=reg, client=bclient
        )
        assert res_b is None and "payload_too_large" in (err_b or "")
        res_r, err_r = await _fetch_source_text(
            "redir", "https://example.com/r", timeout_s=2.0, breakers=reg, client=bclient
        )
        assert res_r is None and "RemoteProtocolError" in (err_r or "")
        res_js, err_js = await _fetch_source_text(
            "redir_js", "https://example.com/js_redir", timeout_s=2.0, breakers=reg, client=bclient
        )
        assert res_js is None and "InvalidURL" in (err_js or "")
        res_ssrf, err_ssrf = await _fetch_source_text(
            "redir_ssrf",
            "https://example.com/ssrf_redir",
            timeout_s=2.0,
            breakers=reg,
            client=bclient,
        )
        assert res_ssrf is None and "ssrf_redirect_blocked" in (err_ssrf or "")

    # 6. Boundary published_at OverflowError must never trigger HTTP 500
    r_ovr = client.post(
        "/v1/market/news-wire",
        json={
            "symbols": ["FPT"],
            "raw_articles": [
                {"title": "FPT boundary Z", "published_at": "9999-12-31T23:00:00Z"},
                {"title": "FPT boundary offset", "published_at": "9999-12-31T23:00:00-05:00"},
            ],
        },
    )
    assert r_ovr.status_code == 200

    # 7. Corrupt unhashable cache entries skip bad item/degrade gracefully
    assert (
        _deserialize_event_records(
            [
                {
                    "event_id": "bad",
                    "title": "X",
                    "event_type": [],
                    "severity": "info",
                    "risk_level": 0,
                    "event_subtype": "s",
                }
            ]
        )
        == []
    )

    class _UnhashableWireCache:
        def __init__(self, payload: Any) -> None:
            self._payload = payload

        async def get_json(self, _k: str) -> Any:
            return self._payload

        async def set_json(self, *_a: Any, **_kw: Any) -> None:
            return None

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(503, text="err"))
    ) as c503:
        w_res = await fetch_market_news_wire(
            lang="vi",
            cache=_UnhashableWireCache({"items": [{"title": "FPT corrupt", "scope": ["ticker"]}]}),
            breakers=EngineBreakerRegistry(),
            client=c503,
        )
        assert w_res["items"][0].title == "FPT corrupt"
        assert w_res["items"][0].scope == "general"
        w_fallback = await fetch_market_news_wire(
            lang="vi",
            cache=_UnhashableWireCache({"items": "corrupt-not-list"}),
            breakers=EngineBreakerRegistry(),
            client=c503,
        )
        assert w_fallback["partial"] is True

    # 8. CancelledError cancels child tasks cleanly
    started_cnt = {"n": 0}
    cancelled_cnt = {"n": 0}

    async def slow_handler(_req: httpx.Request) -> httpx.Response:
        started_cnt["n"] += 1
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            cancelled_cnt["n"] += 1
            raise
        return httpx.Response(200, text="ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow_handler)) as sc:
        t = asyncio.create_task(
            fetch_market_news_wire(
                lang="vi", timeout_s=5.0, breakers=EngineBreakerRegistry(), client=sc
            )
        )
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        await asyncio.sleep(0.02)
        assert started_cnt["n"] > 0 and cancelled_cnt["n"] == started_cnt["n"]
