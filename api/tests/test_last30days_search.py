"""Real search() behavior tests for Last30Days-inspired adapters and the
keyless public-API scrapers (docs/32 §6). All network touches go through
httpx.MockTransport — injected via the ``client=`` kwarg where supported, or
via a monkeypatched module-level ``get_http_client`` for pool-based adapters.
"""

from __future__ import annotations

import httpx
import pytest

from app.retrieval.adapters.base import AdapterError
from app.retrieval.adapters.careers import CareersAdapter
from app.retrieval.adapters.dogceo import DogCEOAdapter
from app.retrieval.adapters.ipapi import IPAPIGeolocationAdapter
from app.retrieval.adapters.opentdb import OpenTriviaAdapter
from app.retrieval.adapters.polymarket import PolymarketAdapter, PolymarketTrendingAdapter
from app.retrieval.adapters.stocktwits import StockTwitsAdapter, StockTwitsTrendingAdapter
from app.retrieval.adapters.uspto import USPTOAdapter, USPTOCompanyAdapter
from app.retrieval.adapters.yahoo_finance import (
    YahooFinanceAdapter,
    _calc_technical_summary,
    _extract_yahoo_lookup_query,
)
from app.retrieval.health import Outcome


def _mock(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def patched_pool(monkeypatch):
    def _patch(module_name: str, handler) -> None:
        module = __import__(module_name, fromlist=["get_http_client"])
        # Fresh client per call — pool-based adapters close clients via async-with.
        monkeypatch.setattr(module, "get_http_client", lambda *a, **kw: _mock(handler))

    return _patch


# --- Careers ---------------------------------------------------------------------


_CAREERS_HTML = """
<html><body>
  <a href="/jobs/ai-research-engineer">AI Research Engineer</a>
  <a href="/careers/platform-lead">Platform Lead</a>
  <a href="https://external.example/apply?x=1">Apply</a>
</body></html>
"""

_JOB_HTML = """
<html><head><title>AI Research Engineer</title></head><body>
  <h1>AI Research Engineer</h1>
  <div class="location">San Francisco, CA</div>
  <div class="date">2026-11-15</div>
  <div class="job-description">We are hiring an expert in machine learning and
  large language model inference to build the next generation of agents. This
  role touches retrieval, embeddings and distributed serving infrastructure.</div>
</body></html>
"""


class TestCareersAdapter:
    async def test_happy_path_scrapes_job_details(self):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.rstrip("/") == "https://openai.com/careers":
                return httpx.Response(200, text=_CAREERS_HTML)
            assert "/jobs/" in url or "/careers/platform-lead" in url
            return httpx.Response(200, text=_JOB_HTML)

        adapter = CareersAdapter(client=_mock(handler))
        results = await adapter.search("openai", max_results=5)
        assert len(results) == 2
        first = results[0]
        assert first.adapter == "careers"
        assert first.title == "Job: AI Research Engineer at openai"
        assert "Location: San Francisco, CA" in first.snippet
        assert "Posted: 2026-11-15" in first.snippet
        assert first.published_at is not None
        assert first.url == "https://openai.com/careers/jobs/ai-research-engineer"

    async def test_non_200_careers_page_returns_empty(self):
        adapter = CareersAdapter(client=_mock(lambda request: httpx.Response(503, text="x")))
        assert await adapter.search("openai") == []

    async def test_no_job_links_returns_empty(self):
        adapter = CareersAdapter(
            client=_mock(lambda request: httpx.Response(200, text="<html><p>nothing</p></html>"))
        )
        assert await adapter.search("openai") == []

    async def test_unknown_company_gets_pattern_url(self):
        seen_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            return httpx.Response(200, text="<html></html>")

        adapter = CareersAdapter(client=_mock(handler))
        assert await adapter.search("acmecorp") == []
        assert seen_urls == ["https://acmecorp.com/careers"]

    async def test_timeout_raises_adapter_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("slow", request=request)

        adapter = CareersAdapter(client=_mock(handler))
        with pytest.raises(AdapterError) as exc_info:
            await adapter.search("openai", timeout_s=1.0)
        assert exc_info.value.kind is Outcome.TIMEOUT

    async def test_transport_error_raises_upstream_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        adapter = CareersAdapter(client=_mock(handler))
        with pytest.raises(AdapterError) as exc_info:
            await adapter.search("openai")
        assert exc_info.value.kind is Outcome.UPSTREAM_ERROR


# --- Polymarket ------------------------------------------------------------------


_SEARCH_BODY = {"markets": [{"id": "mkt-1"}, {"id": "mkt-2"}]}

_MARKET_DETAIL = {
    "id": "mkt-1",
    "question": "Will Bitcoin close above 100k in 2026?",
    "url": "https://polymarket.com/event/btc-100k",
    "createdAt": "2026-10-01T00:00:00Z",
    "outcomes": [
        {"name": "Yes", "probability": 0.65, "volumeUsd": 12000.0},
        {"name": "No", "probability": 0.35, "volumeUsd": 8000.0},
    ],
}


class TestPolymarketAdapter:
    async def test_happy_path_with_details(self):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            assert request.headers["Authorization"] == "Bearer PK"
            if path.endswith("/markets/search"):
                assert request.url.params["query"] == "bitcoin"
                return httpx.Response(200, json=_SEARCH_BODY)
            assert "/markets/mkt-" in path
            return httpx.Response(200, json=dict(_MARKET_DETAIL, id=path.rsplit("-", 1)[-1]))

        adapter = PolymarketAdapter(api_key="PK", client=_mock(handler))
        results = await adapter.search("bitcoin", max_results=2)
        assert len(results) == 2
        first = results[0]
        assert first.title == "Polymarket: Will Bitcoin close above 100k in 2026?"
        assert "Yes: 65% (12,000 USD)" in first.snippet
        assert "$20,000" in first.snippet
        assert first.published_at is not None

    async def test_no_markets_returns_empty(self):
        adapter = PolymarketAdapter(
            client=_mock(lambda r: httpx.Response(200, json={"markets": []}))
        )
        assert await adapter.search("bitcoin") == []

    async def test_rate_limited_raises_retryable(self):
        adapter = PolymarketAdapter(client=_mock(lambda r: httpx.Response(429, text="slow down")))
        with pytest.raises(AdapterError) as exc_info:
            await adapter.search("bitcoin")
        assert exc_info.value.kind is Outcome.RATE_LIMITED
        assert exc_info.value.retryable is True
        assert exc_info.value.retry_after_s == 10.0

    async def test_forbidden_raises_blocked(self):
        adapter = PolymarketAdapter(client=_mock(lambda r: httpx.Response(403, text="no")))
        with pytest.raises(AdapterError) as exc_info:
            await adapter.search("bitcoin")
        assert exc_info.value.kind is Outcome.BLOCKED
        assert exc_info.value.retryable is False

    async def test_server_error_raises_upstream_error(self):
        adapter = PolymarketAdapter(client=_mock(lambda r: httpx.Response(500, text="oops")))
        with pytest.raises(AdapterError) as exc_info:
            await adapter.search("bitcoin")
        assert exc_info.value.kind is Outcome.UPSTREAM_ERROR
        assert exc_info.value.retryable is False


class TestPolymarketTrendingAdapter:
    async def test_happy_path(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/markets/trending")
            return httpx.Response(
                200,
                json={
                    "markets": [
                        dict(_MARKET_DETAIL, id="t-1", question="Fed rate cut in Q1?"),
                    ]
                },
            )

        adapter = PolymarketTrendingAdapter(api_key="PK", client=_mock(handler))
        results = await adapter.search("anything", max_results=5)
        assert len(results) == 1
        assert results[0].title == "Trending: Fed rate cut in Q1?"
        assert results[0].rank == 1

    async def test_rate_limited(self):
        adapter = PolymarketTrendingAdapter(client=_mock(lambda r: httpx.Response(429, text="x")))
        with pytest.raises(AdapterError) as exc_info:
            await adapter.search("anything")
        assert exc_info.value.kind is Outcome.RATE_LIMITED


# --- StockTwits ------------------------------------------------------------------


_TW_MESSAGE = {
    "id": 555,
    "body": "$AAPL breaking out — biggest volume this month",
    "sentiment": {"basic": "Bullish", "score": 0.6},
    "likes": {"total": 42},
    "conversation": {"total_replies": 7},
    "created_at": 1760000000000,
    "user": {"username": "trader_joe"},
}


class TestStockTwitsAdapter:
    async def test_happy_path(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "/api/2/streams/symbol/AAPL.json" in str(request.url)
            assert request.url.params["filter"] == "all"
            return httpx.Response(200, json={"messages": [_TW_MESSAGE]})

        adapter = StockTwitsAdapter(api_key="SK", client=_mock(handler))
        results = await adapter.search("AAPL sentiment", max_results=5)
        assert len(results) == 1
        r = results[0]
        assert r.title == "StockTwits: AAPL"
        assert "🟢 trader_joe" in r.snippet
        assert "42 likes, 7 replies" in r.snippet
        assert r.published_at is not None

    async def test_query_without_symbol_returns_empty(self):
        seen = {"called": False}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["called"] = True
            return httpx.Response(200, json={"messages": []})

        adapter = StockTwitsAdapter(client=_mock(handler))
        assert await adapter.search("holdings today") == []
        assert seen["called"] is False

    async def test_symbol_not_found_returns_empty(self):
        adapter = StockTwitsAdapter(client=_mock(lambda r: httpx.Response(404, json={})))
        assert await adapter.search("ZZZZ") == []

    async def test_upstream_error_swallowed_per_symbol(self):
        # A failing symbol is skipped (AdapterError caught per-symbol); with a
        # single symbol the search degrades to empty results.
        adapter = StockTwitsAdapter(client=_mock(lambda r: httpx.Response(500, text="boom")))
        assert await adapter.search("MSFT") == []

    async def test_transport_error_swallowed_per_symbol(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        adapter = StockTwitsAdapter(client=_mock(handler))
        assert await adapter.search("MSFT") == []


class TestStockTwitsTrendingAdapter:
    async def test_happy_path(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "/api/2/trending/symbols.json" in str(request.url)
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {"symbol": "TSLA", "title": "Tesla", "messages": 900, "followers": 12}
                    ]
                },
            )

        adapter = StockTwitsTrendingAdapter(client=_mock(handler))
        results = await adapter.search("trending")
        assert len(results) == 1
        assert results[0].url == "https://stocktwits.com/TSLA"
        assert "900 messages" in results[0].snippet

    async def test_non_200_returns_empty(self):
        adapter = StockTwitsTrendingAdapter(client=_mock(lambda r: httpx.Response(500, text="x")))
        assert await adapter.search("trending") == []


# --- USPTO -----------------------------------------------------------------------


class TestUSPTOAdapter:
    async def test_happy_path(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "/patent/search" in str(request.url)
            params = request.url.params
            assert "assignee:" in params["searchText"]  # "Inc" triggers assignee mode
            assert params["rows"] == "10"  # min(max_results=10, 25)
            assert "X-Api-Key" not in request.headers  # no key configured
            return httpx.Response(
                200,
                json={
                    "patents": [
                        {
                            "patentNumber": "US1234567",
                            "patentTitle": "Multimodal retrieval system",
                            "filingDate": "2026-11-10",
                            "grantDate": "2026-11-20",
                            "patentAbstract": "A system for retrieval augmented generation "
                            "that fuses sparse and dense evidence with citation grounding for "
                            "enterprise search workloads at scale.",
                            "inventor": [{"name": "Ada Lovelace"}, {"name": "Grace Hopper"}],
                            "assignee": [{"name": "Acme Inc"}],
                        }
                    ]
                },
            )

        adapter = USPTOAdapter(client=_mock(handler))
        results = await adapter.search("Acme Inc", max_results=10)
        assert len(results) == 1
        r = results[0]
        assert r.title == "Patent: Multimodal retrieval system"
        assert r.url == "https://patft.uspto.gov/netacgi/nph-Parser?patentnumber=US1234567"
        assert "Assignee: Acme Inc" in r.snippet
        assert "Inventor: Ada Lovelace, Grace Hopper" in r.snippet
        assert "Filed: 2026-11-10" in r.snippet
        assert r.published_at is not None

    async def test_freshness_window_default_30_days(self):
        def handler(request: httpx.Request) -> httpx.Response:
            filt = request.url.params["filter"]
            assert "patentDate: [" in filt
            return httpx.Response(200, json={"patents": []})

        adapter = USPTOAdapter(client=_mock(handler))
        assert await adapter.search("quantum sensor") == []

    async def test_rate_limited_raises(self):
        adapter = USPTOAdapter(client=_mock(lambda r: httpx.Response(429, text="slow")))
        with pytest.raises(AdapterError) as exc_info:
            await adapter.search("Acme Inc")
        assert exc_info.value.kind is Outcome.RATE_LIMITED
        assert exc_info.value.retry_after_s == 30.0

    async def test_forbidden_raises_blocked(self):
        adapter = USPTOAdapter(client=_mock(lambda r: httpx.Response(403, text="no key")))
        with pytest.raises(AdapterError) as exc_info:
            await adapter.search("Acme Inc")
        assert exc_info.value.kind is Outcome.BLOCKED

    async def test_server_error_raises_upstream_error(self):
        adapter = USPTOAdapter(client=_mock(lambda r: httpx.Response(503, text="down")))
        with pytest.raises(AdapterError) as exc_info:
            await adapter.search("Acme Inc")
        assert exc_info.value.kind is Outcome.UPSTREAM_ERROR
        assert exc_info.value.retryable is False

    async def test_company_adapter_delegates(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["X-Api-Key"] == "UK"
            return httpx.Response(200, json={"patents": []})

        adapter = USPTOCompanyAdapter(api_key="UK", client=_mock(handler))
        assert await adapter.search("Acme Inc") == []


# --- IP-API geolocation ----------------------------------------------------------


_IPAPI_GEO = {
    "status": "success",
    "query": "8.8.8.8",
    "country": "United States",
    "regionName": "Virginia",
    "city": "Ashburn",
    "lat": 39.03,
    "lon": -77.5,
    "timezone": "America/New_York",
    "isp": "Google LLC",
    "as": "AS15169 Google LLC",
}


class TestIPAPIAdapter:
    async def test_ip_query_happy_path(self, patched_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "ip-api.com/json/8.8.8.8" in str(request.url)
            return httpx.Response(200, json=_IPAPI_GEO)

        patched_pool("app.retrieval.adapters.ipapi", handler)
        results = await IPAPIGeolocationAdapter().search("server at 8.8.8.8 status")
        assert len(results) == 1
        r = results[0]
        assert r.title == "Geolocation: United States, Ashburn, Virginia (8.8.8.8)"
        assert "Coordinates: 39.03, -77.5" in r.snippet
        assert "ISP: Google LLC" in r.snippet
        assert r.url == "http://ip-api.com/#8.8.8.8"

    async def test_domain_query_resolved_by_upstream(self, patched_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "ip-api.com/json/example.com" in str(request.url)
            return httpx.Response(200, json=dict(_IPAPI_GEO, query="93.184.216.34"))

        patched_pool("app.retrieval.adapters.ipapi", handler)
        results = await IPAPIGeolocationAdapter().search("example.com")
        assert len(results) == 1

    async def test_failed_status_returns_empty(self, patched_pool):
        patched_pool(
            "app.retrieval.adapters.ipapi",
            lambda r: httpx.Response(200, json={"status": "fail", "message": "reserved range"}),
        )
        assert await IPAPIGeolocationAdapter().search("127.0.0.1") == []

    async def test_transport_error_degrades_to_empty(self, patched_pool):
        # _fetch_geolocation swallows any Exception -> None -> empty results.
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        patched_pool("app.retrieval.adapters.ipapi", handler)
        assert await IPAPIGeolocationAdapter().search("8.8.8.8") == []


# --- Open Trivia DB --------------------------------------------------------------


class TestOpenTriviaAdapter:
    async def test_happy_path_decodes_entities(self, patched_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            assert "opentdb.com/api.php" in url
            assert "category=17" in url  # "science" maps to 17
            assert "difficulty=easy" in url
            assert "type=multiple" in url
            return httpx.Response(
                200,
                json={
                    "response_code": 0,
                    "results": [
                        {
                            "category": "Science: Computers",
                            "difficulty": "easy",
                            "type": "multiple",
                            "question": "What is 2 &amp; 2?",
                            "correct_answer": "4 &lt;bits&gt;",
                            "incorrect_answers": ["5", "3"],
                        }
                    ],
                },
            )

        patched_pool("app.retrieval.adapters.opentdb", handler)
        results = await OpenTriviaAdapter().search("science easy multiple choice", max_results=5)
        assert len(results) == 1
        r = results[0]
        assert r.title == "Trivia: Science: Computers (Easy, Multiple)"
        assert "Q: What is 2 & 2?" in r.snippet
        assert "A: 4 <bits>" in r.snippet
        assert r.url == "https://opentdb.com/"

    async def test_no_results_response_code_returns_empty(self, patched_pool):
        patched_pool(
            "app.retrieval.adapters.opentdb",
            lambda r: httpx.Response(200, json={"response_code": 1, "results": []}),
        )
        assert await OpenTriviaAdapter().search("history") == []

    async def test_server_error_returns_empty(self, patched_pool):
        patched_pool("app.retrieval.adapters.opentdb", lambda r: httpx.Response(500, text="boom"))
        assert await OpenTriviaAdapter().search("history") == []

    async def test_transport_error_degrades_to_empty(self, patched_pool):
        # _fetch_trivia swallows any Exception -> None -> empty results.
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        patched_pool("app.retrieval.adapters.opentdb", handler)
        assert await OpenTriviaAdapter().search("history") == []


# --- Dog CEO ---------------------------------------------------------------------


class TestDogCEOAdapter:
    async def test_random_images(self, patched_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "breeds/image/random/2" in str(request.url)
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "message": ["https://images.dog/a.jpg", "https://images.dog/b.jpg"],
                },
            )

        patched_pool("app.retrieval.adapters.dogceo", handler)
        results = await DogCEOAdapter().search("random dog", max_results=2)
        assert len(results) == 2
        assert results[0].title == "Random Dog"
        assert results[0].url == "https://images.dog/a.jpg"

    async def test_breeds_list(self, patched_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "breeds/list/all" in str(request.url)
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "message": {"hound": ["afghan", "basset"], "poodle": ["toy"]},
                },
            )

        patched_pool("app.retrieval.adapters.dogceo", handler)
        results = await DogCEOAdapter().search("list all breeds")
        assert len(results) == 1
        # 3 sub-breeds + 2 parent breeds = 5 total
        assert results[0].snippet == "Total breeds: 5 | Sample: hound, poodle"

    async def test_breed_images(self, patched_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "breed/hound/images/random" in str(request.url)
            return httpx.Response(
                200, json={"status": "success", "message": ["https://images.dog/h1.jpg"]}
            )

        patched_pool("app.retrieval.adapters.dogceo", handler)
        results = await DogCEOAdapter().search("hound")
        assert len(results) == 1
        assert results[0].title == "Hound Dog"

    async def test_unknown_breed_falls_back_to_random(self, patched_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            if "breed/notadog/" in str(request.url):
                return httpx.Response(404, json={"status": "error"})
            assert "breeds/image/random/1" in str(request.url)
            return httpx.Response(
                200, json={"status": "success", "message": ["https://images.dog/r.jpg"]}
            )

        patched_pool("app.retrieval.adapters.dogceo", handler)
        results = await DogCEOAdapter().search("notadog")
        assert len(results) == 1
        assert results[0].title == "Random Dog (Breed not found)"


# --- Yahoo Finance ---------------------------------------------------------------


def _chart_payload():
    closes = [100.0 + i for i in range(60)]  # 60 bars -> SMA50 + trend resolvable
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "regularMarketPrice": 169.0,  # above SMA20 & SMA50 -> Bullish trend
                        "chartPreviousClose": 99.0,
                        "currency": "USD",
                        "fullExchangeName": "NasdaqGS",
                        "fiftyTwoWeekHigh": 150.0,
                        "fiftyTwoWeekLow": 90.0,
                    },
                    "indicators": {
                        "quote": [
                            {
                                "close": closes,
                                "volume": [1_000_000.0] * 60,
                                "high": [101.0 + i for i in range(60)],
                                "low": [99.0 + i for i in range(60)],
                            }
                        ]
                    },
                }
            ]
        }
    }


class TestYahooFinanceAdapter:
    async def test_happy_path_quotes_news_and_chart_techs(self):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "v1/finance/search" in url:
                assert request.url.params["q"] == "FPT"
                return httpx.Response(
                    200,
                    json={
                        "quotes": [
                            {
                                "symbol": "FPT",
                                "longname": "FPT Corporation",
                                "exchDisp": "HNX",
                                "typeDisp": "Equity",
                                "sectorDisp": "Technology",
                                "regularMarketTime": 1760000000,
                            }
                        ],
                        "news": [
                            {
                                "link": "https://finance.example/fpt-news",
                                "title": "FPT wins AI contract",
                                "publisher": "CafeF",
                                "providerPublishTime": 1760000000,
                                "relatedTickers": ["FPT"],
                            }
                        ],
                    },
                )
            assert "/v8/finance/chart/FPT" in url
            return httpx.Response(200, json=_chart_payload())

        adapter = YahooFinanceAdapter(client=_mock(handler))
        results = await adapter.search("FPT stock", max_results=5)
        assert len(results) == 2
        quote = results[0]
        assert quote.title == "FPT - FPT Corporation (HNX)"
        assert "[FPT · Equity]" in quote.snippet
        assert "Price: 169.00 USD (+70.71%)" in quote.snippet
        assert "52w Range: 90.00 - 150.00" in quote.snippet
        assert "RSI(14)=" in quote.snippet
        assert "Trend: Bullish" in quote.snippet
        assert "Vol=" in quote.snippet
        news = results[1]
        assert news.title == "FPT wins AI contract"
        assert news.source_name == "CafeF"
        assert news.published_at is not None

    async def test_rate_limited(self):
        adapter = YahooFinanceAdapter(client=_mock(lambda r: httpx.Response(429, text="x")))
        with pytest.raises(AdapterError) as exc_info:
            await adapter.search("FPT")
        assert exc_info.value.kind is Outcome.RATE_LIMITED

    async def test_forbidden(self):
        adapter = YahooFinanceAdapter(client=_mock(lambda r: httpx.Response(403, text="x")))
        with pytest.raises(AdapterError) as exc_info:
            await adapter.search("FPT")
        assert exc_info.value.kind is Outcome.BLOCKED

    async def test_server_error_upstream(self):
        adapter = YahooFinanceAdapter(client=_mock(lambda r: httpx.Response(502, text="x")))
        with pytest.raises(AdapterError) as exc_info:
            await adapter.search("FPT")
        assert exc_info.value.kind is Outcome.UPSTREAM_ERROR

    async def test_non_json_body_raises(self):
        adapter = YahooFinanceAdapter(
            client=_mock(lambda r: httpx.Response(200, text="<html>not json</html>"))
        )
        with pytest.raises(AdapterError, match="non-JSON"):
            await adapter.search("FPT")

    async def test_empty_query_returns_empty(self):
        adapter = YahooFinanceAdapter(client=_mock(lambda r: pytest.fail("no call expected")))
        assert await adapter.search("   ") == []

    async def test_rss_enrichment_without_injected_client(self, patched_pool):
        """When no client is injected the adapter additionally fetches the
        ticker RSS feed; stale items beyond freshness_days are dropped."""
        rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel>
            <item>
                <title>FPT announces new AI lab</title>
                <link>https://finance.example/fpt-ai-lab</link>
                <description>Built a new research hub.</description>
                <pubDate>Mon, 16 Nov 2026 08:00:00 GMT</pubDate>
            </item>
            <item>
                <title>FPT old story</title>
                <link>https://finance.example/fpt-old</link>
                <description>From 2020.</description>
                <pubDate>Mon, 16 Nov 2020 08:00:00 GMT</pubDate>
            </item>
        </channel></rss>"""

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "v1/finance/search" in url:
                return httpx.Response(200, json={"quotes": [{"symbol": "FPT"}], "news": []})
            if "/v8/finance/chart/" in url:
                return httpx.Response(500, text="chart down")
            assert "feeds.finance.yahoo.com" in url
            return httpx.Response(200, text=rss_xml)

        patched_pool("app.retrieval.adapters.yahoo_finance", handler)
        adapter = YahooFinanceAdapter()  # no injected client -> RSS path active
        results = await adapter.search("FPT", freshness_days=7, max_results=5)
        urls = [r.url for r in results]
        assert "https://finance.example/fpt-ai-lab" in urls
        assert "https://finance.example/fpt-old" not in urls  # stale beyond cutoff
        assert results[-1].source_name == "Yahoo Finance"


class TestYahooFinanceHelpers:
    def test_extract_query_cashtag(self):
        assert _extract_yahoo_lookup_query("$fpt stock") == ("FPT", "FPT")

    def test_extract_query_dotted_ticker(self):
        assert _extract_yahoo_lookup_query("FPT.VN") == ("FPT.VN", "FPT.VN")

    def test_extract_query_vietnamese_ticker(self):
        # Vietnamese diacritics + VN ticker -> .VN suffix hint
        assert _extract_yahoo_lookup_query("FPT cổ phiếu") == ("FPT.VN", "FPT.VN")

    def test_extract_query_noise_stripped(self):
        q, hint = _extract_yahoo_lookup_query("apple stock price news")
        assert q == "apple"
        assert hint is None

    def test_technical_summary_overbought_and_trend(self):
        closes = [float(100 + 2 * i) for i in range(60)]  # monotonic rally
        summary = _calc_technical_summary(closes, [1000.0] * 60, current_price=closes[-1])
        assert "RSI(14)=" in summary
        assert "Overbought" in summary
        assert "SMA20=" in summary
        assert "SMA50=" in summary
        assert "Trend: Bullish" in summary
        assert "Vol=" in summary

    def test_technical_summary_empty_without_closes(self):
        assert _calc_technical_summary([], [], None) == ""
