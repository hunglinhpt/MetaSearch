"""Events-module coverage round 2 (docs/30): Google-News fallback tagging,
caller-supplied notice promotion, cache round-trips and parse-error paths."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.cache import CacheClient
from app.retrieval.events import (
    CorporateEventRecord,
    EngineBreakerRegistry,
    _deserialize_event_records,
    _serialize_event_records,
    fetch_market_events,
    fetch_market_news_wire,
)


@pytest.fixture(autouse=True)
def _reset_cpu_semaphore():
    """The module-level CPU-offload semaphore binds to the first event loop
    that queues >2 concurrent run_cpu calls; these fan-out tests do exactly
    that, so reset it around every test to avoid cross-loop RuntimeError leaks."""
    from app.core.offload import reset_cpu_semaphore

    reset_cpu_semaphore()
    yield
    reset_cpu_semaphore()


def _recent_rfc(hours_ago: float = 2.0) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).strftime("%a, %d %b %Y %H:%M:%S +0000")


def _gn_rss(*titles_urls: tuple[str, str]) -> str:
    items = "".join(
        f"""
        <item>
          <title>{title}</title>
          <link>{url}</link>
          <pubDate>{_recent_rfc()}</pubDate>
          <description>Cập nhật thị trường chứng khoán mới nhất.</description>
        </item>"""
        for title, url in titles_urls
    )
    return (
        f"<?xml version='1.0' encoding='utf-8'?><rss version='2.0'><channel>{items}</channel></rss>"
    )


# --- wire: Google-News search-fallback path ---------------------------------------


@pytest.mark.asyncio
async def test_wire_global_symbol_search_fallback_tags_hits():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "news.google.com" in url:
            assert "NVDA" in url  # per-symbol fallback query
            return httpx.Response(
                200,
                text=_gn_rss(
                    (
                        "SEC enforcement: NVDA fined for disclosure violation",
                        "https://sec.example/nvda-fine",
                    ),
                    ("Cooking show season finale", "https://life.example/cooking"),
                ),
            )
        # every other wire source (vi wire skipped for global-only query) fails fast
        return httpx.Response(503, text="offline")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_client:
        wire = await fetch_market_news_wire(
            symbols=["NVDA"],
            lang="en",
            include_search_fallback=True,
            include_market_macro=False,
            breakers=EngineBreakerRegistry(),
            client=mock_client,
            timeout_s=8.0,
        )
    assert "google_news" in wire["sources_used"]
    nvda_items = [it for it in wire["items"] if "NVDA" in it.symbols]
    assert nvda_items, "boundary-pattern tagging must bind NVDA to the enforcement hit"
    assert nvda_items[0].category == "regulatory"
    assert nvda_items[0].severity == "critical"
    # the irrelevant hit must not be tagged with NVDA
    assert all("Cooking" not in it.title for it in nvda_items)


@pytest.mark.asyncio
async def test_wire_broad_queries_when_no_symbols():
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "news.google.com" in str(request.url):
            seen_queries.append(request.url.params.get("q", ""))
            return httpx.Response(
                200,
                text=_gn_rss(
                    ("Stock market rally continues", "https://markets.example/rally"),
                ),
            )
        return httpx.Response(503, text="offline")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_client:
        wire = await fetch_market_news_wire(
            lang="auto",
            include_search_fallback=True,
            breakers=EngineBreakerRegistry(),
            client=mock_client,
            timeout_s=8.0,
        )
    # auto language fans out BOTH broad en and vi queries
    assert any("stock market" in q.lower() for q in seen_queries)
    assert any("chứng khoán" in q for q in seen_queries)
    assert "google_news" in wire["sources_used"]


# --- events: caller-supplied notice promotion branches -----------------------------


@pytest.mark.asyncio
async def test_events_raw_notices_promote_all_event_types():
    notices = [
        {
            "title": "UBCKNN xử phạt công ty FPT vi phạm công bố thông tin",
            "symbol": "FPT",
            "event_type": "regulatory",
            "source_id": "ssc",
            "source_url": "https://ssc.gov.vn/fpt-penalty",
            "event_date": "2026-11-10",
        },
        {
            "title": "Cổ đông lớn đăng ký bán 2.000.000 cổ phiếu HPG",
            "symbol": "HPG",
            "event_type": "insider",
        },
        {
            "title": "Sở GDCK Hà Nội đình chỉ giao dịch cổ phiếu XYZ kể từ 12/11/2026",
            "symbol": "XYZ",
            "event_type": "listing",
        },
        {
            "title": "CTCP ABC bị cảnh báo về bệnh thuốc: thông tin kiểm soát",
            "symbol": "ABC",
        },  # no hint, regulatory probe risk 2 (warning) -> branch risk>=1
        {
            "title": "FPT chốt quyền cổ tức bằng cổ phiếu tỷ lệ 10:2",
            "symbol": "FPT",
            "ex_date": "2026-12-01",
            "record_date": "2026-11-25",
            "payment_date": "2026-12-15",
        },
    ]
    result = await fetch_market_events(raw_notices=notices, symbols=["FPT", "HPG", "XYZ", "ABC"])
    events: list[CorporateEventRecord] = result["events"]
    by_type: dict[str, list[CorporateEventRecord]] = {}
    for ev in events:
        by_type.setdefault(ev.event_type, []).append(ev)
    assert "regulatory" in by_type and "insider" in by_type
    assert "listing" in by_type and "dividend_calendar" in by_type
    reg = by_type["regulatory"][0]
    assert reg.risk_level == 3 and reg.event_subtype == "sanction"
    assert reg.source_url == "https://ssc.gov.vn/fpt-penalty"
    insider = by_type["insider"][0]
    assert insider.event_subtype == "sell_planned"
    assert insider.shares_delta == 2_000_000.0
    corp = by_type["dividend_calendar"][0]
    assert corp.is_share_changing and corp.share_factor == pytest.approx(1.2)
    assert corp.ex_date == "2026-12-01" and corp.payment_date == "2026-12-15"


@pytest.mark.asyncio
async def test_events_raw_notices_without_symbols_still_extract():
    # no explicit symbol -> extraction from text (registered ticker in title)
    notices = [
        {
            "title": "FPT công bố phát hành cổ phiếu cho ESOP 1.500.000 cp",
            "event_type": "corporate",
        },
    ]
    result = await fetch_market_events(raw_notices=notices)
    assert result["events"], "text extraction must find FPT"
    assert result["events"][0].symbol == "FPT"
    assert result["sources_used"] == ["caller_supplied"]


# --- events: network path with cache round-trip and error paths -------------------


def _events_handler(cadef_json_errors: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "cafef.vn/du-lieu/Ajax" in url:
            if cadef_json_errors:
                return httpx.Response(200, text="<html>not json</html>")
            return httpx.Response(
                200,
                json={
                    "Data": [
                        {"Time": "/Date(1782406800000)/", "Text": "FPT: cổ tức bằng cổ phiếu 10:2"},
                        {"Time": "2026-11-20", "Text": "FPT: cổ tức tiền mặt 20%, đkcc 25/11/2026"},
                    ]
                },
            )
        if "cotuc.vn" in url:
            return httpx.Response(
                200,
                text=(
                    "<html><body>"
                    '<a href="/su-kien/2026-12-01/fpt-co-tuc-tien-mat">'
                    "<span>FPT</span>"
                    "<span>Cổ tức tiền mặt 20% đkcc 25/11/2026 thanh toán 15/12/2026</span>"
                    "</a></body></html>"
                ),
            )
        if "news.google.com" in url:
            return httpx.Response(
                200,
                text=_gn_rss(
                    (
                        "UBCKNN xử phạt FPT vi phạm",
                        "https://ssc.gov.vn/fpt-penalty",
                    ),
                ),
            )
        # every RSS wire source returns a valid minimal feed with one item
        return httpx.Response(
            200,
            text=_gn_rss(("FPT công bố kết quả kinh doanh quý", "https://vietstock.vn/fpt-q.htm")),
        )

    return handler


@pytest.mark.asyncio
async def test_events_network_path_with_cache_roundtrip():
    cache = CacheClient()

    async with httpx.AsyncClient(transport=httpx.MockTransport(_events_handler())) as mock_client:
        first = await fetch_market_events(
            symbols=["FPT"],
            breakers=EngineBreakerRegistry(),
            client=mock_client,
            cache=cache,
            timeout_s=8.0,
        )
        assert first["partial"] is False
        assert any(ev.symbol == "FPT" for ev in first["events"])
        cafef_cached = [ev for ev in first["events"] if ev.source_id == "cafef_events"]
        assert cafef_cached, "CafeF events must be fetched and parsed"
        # second run: everything served from the in-memory cache
        second = await fetch_market_events(
            symbols=["FPT"],
            breakers=EngineBreakerRegistry(),
            client=mock_client,
            cache=cache,
            timeout_s=8.0,
        )
        assert [ev.event_id for ev in second["events"]] == [ev.event_id for ev in first["events"]]


@pytest.mark.asyncio
async def test_events_network_parse_error_paths():
    handler = _events_handler(cadef_json_errors=True)
    seen_urls: list[str] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if "vietstock.vn" in str(request.url):
            return httpx.Response(200, text="this is not xml at all <<<")
        return handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(wrapped)) as mock_client:
        result = await fetch_market_events(
            symbols=["FPT"],
            breakers=EngineBreakerRegistry(threshold=100),
            client=mock_client,
            timeout_s=8.0,
        )
    assert result["partial"] is True
    assert any("bad_json" in s for s in result["sources_failed"])
    assert any("xml_parse_error" in s for s in result["sources_failed"])


# --- serialize / deserialize round-trip -------------------------------------------


def test_event_record_serialization_roundtrip():
    record = CorporateEventRecord(
        event_id="cafef:FPT:2026-12-01:abc",
        symbol="FPT",
        exchange="HOSE",
        event_type="corporate_action",
        event_subtype="share_change",
        severity="high",
        risk_level=1,
        title="FPT thưởng cổ phiếu 10:2",
        detail="chi tiết",
        event_date="2026-12-01",
        ex_date="2026-12-01",
        record_date="2026-11-25",
        payment_date="2026-12-15",
        source_id="cafef_events",
        source_url="https://cafef.vn/x",
        is_share_changing=True,
        share_factor=1.2,
        is_dilution=True,
        cash_dividend_pct=10.0,
        cash_per_share_vnd=1000.0,
        stock_ratio="10:2",
    )
    blob = _serialize_event_records([record])
    assert isinstance(blob, list) and blob[0]["symbol"] == "FPT"
    restored = _deserialize_event_records(blob)
    assert len(restored) == 1
    back = restored[0]
    assert back.event_id == record.event_id
    assert back.share_factor == pytest.approx(1.2)
    assert back.is_share_changing is True
    assert back.cash_dividend_pct == pytest.approx(10.0)
    # malformed entries are skipped, not raised
    assert _deserialize_event_records(["nope", {"event_id": "x"}]) == []
