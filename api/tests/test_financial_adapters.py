"""Financial adapters tests: Yahoo Finance and SEC EDGAR."""

from __future__ import annotations

import httpx
import pytest

from app.orchestration.research import detect_profile
from app.retrieval.adapters.base import AdapterError
from app.retrieval.adapters.sec_edgar import SecEdgarAdapter
from app.retrieval.adapters.yahoo_finance import YahooFinanceAdapter


@pytest.fixture
def patched_httpx(monkeypatch):
    original = httpx.AsyncClient

    def _patch(module_name: str, handler):
        module = __import__(module_name, fromlist=["httpx"])

        def make_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        monkeypatch.setattr(module.httpx, "AsyncClient", make_client)

    return _patch


class TestYahooFinance:
    async def test_empty_query(self):
        adapter = YahooFinanceAdapter()
        assert await adapter.search("   ") == []

    async def test_parses_quotes_and_enriches_chart(self, patched_httpx):
        def handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "/v1/finance/search" in url_str:
                return httpx.Response(
                    200,
                    json={
                        "quotes": [
                            {
                                "symbol": "NVDA",
                                "shortname": "NVIDIA Corp",
                                "longname": "NVIDIA Corporation",
                                "exchange": "NMS",
                                "exchDisp": "NASDAQ",
                                "typeDisp": "Equity",
                                "sectorDisp": "Technology",
                            }
                        ],
                        "news": [
                            {
                                "title": "Nvidia Soars on AI Chip Demand",
                                "publisher": "Bloomberg",
                                "link": "https://finance.yahoo.com/news/nvidia-soars.html",
                                "providerPublishTime": 1730000000,
                                "relatedTickers": ["NVDA"],
                            }
                        ],
                    },
                )
            elif "/v8/finance/chart/NVDA" in url_str:
                return httpx.Response(
                    200,
                    json={
                        "chart": {
                            "result": [
                                {
                                    "meta": {
                                        "symbol": "NVDA",
                                        "regularMarketPrice": 140.50,
                                        "chartPreviousClose": 135.00,
                                        "fiftyTwoWeekHigh": 144.00,
                                        "fiftyTwoWeekLow": 45.00,
                                        "currency": "USD",
                                    }
                                }
                            ]
                        }
                    },
                )
            return httpx.Response(404)

        adapter = YahooFinanceAdapter()
        patched_httpx("app.retrieval.adapters.yahoo_finance", handler)
        results = await adapter.search("$NVDA", max_results=10)

        assert len(results) == 2
        # Check Quote
        q_res = results[0]
        assert "NVDA - NVIDIA Corporation" in q_res.title
        assert "140.50 USD" in q_res.snippet
        assert "+4.07%" in q_res.snippet
        assert "52w Range: 45.00 - 144.00" in q_res.snippet
        assert q_res.url == "https://finance.yahoo.com/quote/NVDA/"
        assert q_res.source_engines == ["yahoo_finance"]

        # Check News
        n_res = results[1]
        assert n_res.title == "Nvidia Soars on AI Chip Demand"
        assert "[Bloomberg]" in n_res.snippet
        assert n_res.url == "https://finance.yahoo.com/news/nvidia-soars.html"

    async def test_yahoo_finance_handles_chart_failure_gracefully(self, patched_httpx):
        def handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "/v1/finance/search" in url_str:
                return httpx.Response(
                    200,
                    json={
                        "quotes": [
                            {
                                "symbol": "FPT.VN",
                                "shortname": "FPT Corp",
                                "exchange": "HOSE",
                                "typeDisp": "Equity",
                            }
                        ],
                        "news": [],
                    },
                )
            # Chart fails with 500
            return httpx.Response(500)

        adapter = YahooFinanceAdapter()
        patched_httpx("app.retrieval.adapters.yahoo_finance", handler)
        results = await adapter.search("FPT.VN")
        assert len(results) == 1
        assert "FPT.VN - FPT Corp" in results[0].title
        assert "Exchange: HOSE" in results[0].snippet


class TestSecEdgar:
    async def test_empty_query(self):
        adapter = SecEdgarAdapter()
        assert await adapter.search("   ") == []

    async def test_parses_filings(self, patched_httpx):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "hits": {
                        "hits": [
                            {
                                "_source": {
                                    "ciks": ["0000320193"],
                                    "adsh": "0000320193-24-000106",
                                    "root_form": "10-K",
                                    "file_date": "2024-11-01",
                                    "period_ending": "2024-09-28",
                                    "display_names": ["Apple Inc. (AAPL) (CIK 0000320193)"],
                                }
                            }
                        ]
                    }
                },
            )

        adapter = SecEdgarAdapter()
        patched_httpx("app.retrieval.adapters.sec_edgar", handler)
        results = await adapter.search("Apple 10-K")
        assert len(results) == 1
        assert "Apple Inc. (AAPL)" in results[0].title
        assert "Form 10-K" in results[0].title
        assert "[SEC Form 10-K]" in results[0].snippet
        assert "2024-11-01" in results[0].snippet
        assert "320193" in results[0].url
        assert results[0].source_engines == ["sec_edgar"]

    async def test_sec_edgar_http_error(self, patched_httpx):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text="Too many requests")

        adapter = SecEdgarAdapter()
        patched_httpx("app.retrieval.adapters.sec_edgar", handler)
        with pytest.raises(AdapterError, match="429"):
            await adapter.search("Tesla")


class TestFinancialScaffolding:
    def test_detect_profile_financial(self):
        assert detect_profile("$NVDA stock analysis") == "financial"
        assert detect_profile("Báo cáo tài chính FPT 2024 lợi nhuận P/E") == "financial"
        assert detect_profile("Cổ phiếu Vinamilk VNM tỷ lệ cổ tức") == "financial"
        assert detect_profile("SEC EDGAR 10-K Apple filing") == "financial"
        assert detect_profile("Chỉ số VN-Index hôm nay") == "financial"
        assert detect_profile("Tesla stock valuation DCF model") == "financial"
