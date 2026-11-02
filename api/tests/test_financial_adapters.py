"""Financial adapters tests: Yahoo Finance and SEC EDGAR."""

from __future__ import annotations

import httpx
import pytest

from app.orchestration.research import detect_profile
from app.retrieval.adapters.base import AdapterError
from app.retrieval.adapters.sec_edgar import SecEdgarAdapter
from app.retrieval.adapters.yahoo_finance import (
    YahooFinanceAdapter,
    _calc_rsi,
    _calc_sma,
    _calc_technical_summary,
)


@pytest.fixture
def patched_httpx(monkeypatch):
    def _patch(module_name: str, handler):
        module = __import__(module_name, fromlist=["get_http_client", "httpx"])
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        if hasattr(module, "get_http_client"):
            monkeypatch.setattr(module, "get_http_client", lambda *a, **kw: mock_client)
        if hasattr(module, "httpx"):
            monkeypatch.setattr(module.httpx, "AsyncClient", lambda *a, **kw: mock_client)

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
                                    },
                                    "indicators": {
                                        "quote": [
                                            {
                                                "close": [
                                                    100.0,
                                                    101.5,
                                                    103.0,
                                                    104.5,
                                                    106.0,
                                                    107.5,
                                                    109.0,
                                                    110.5,
                                                    112.0,
                                                    113.5,
                                                    115.0,
                                                    116.5,
                                                    118.0,
                                                    119.5,
                                                    121.0,
                                                    122.5,
                                                    124.0,
                                                    125.5,
                                                    127.0,
                                                    128.5,
                                                    130.0,
                                                    131.5,
                                                    133.0,
                                                    134.5,
                                                    136.0,
                                                    137.5,
                                                    139.0,
                                                    140.5,
                                                    142.0,
                                                    143.5,
                                                ],
                                                "volume": [
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                    10000000,
                                                ],
                                            }
                                        ]
                                    },
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
        assert "Techs: " in q_res.snippet
        assert "RSI(14)=" in q_res.snippet
        assert "SMA20=" in q_res.snippet
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

    def test_technical_indicators_math(self):
        # SMA test
        prices = [10.0, 20.0, 30.0, 40.0, 50.0]
        assert _calc_sma(prices, 3) == 40.0
        assert _calc_sma(prices, 10) is None
        assert _calc_sma([], 3) is None

        # RSI test: Strictly increasing sequence should yield high RSI
        up_prices = [10.0 + i for i in range(25)]
        rsi_up = _calc_rsi(up_prices, 14)
        assert rsi_up is not None and rsi_up > 90.0

        # Strictly decreasing sequence should yield low RSI
        down_prices = [100.0 - i for i in range(25)]
        rsi_down = _calc_rsi(down_prices, 14)
        assert rsi_down is not None and rsi_down < 10.0

        # Technical summary string
        summary = _calc_technical_summary(up_prices, [1000] * 25, 35.0)
        assert "RSI(14)=" in summary
        assert "SMA20=" in summary
        assert "Vol=" in summary


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
