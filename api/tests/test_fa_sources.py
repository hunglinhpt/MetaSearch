"""Multi-source FA (docs/15): direct adapters + aggregator cross-validation.

Every network touch is faked with httpx.MockTransport injected via the
constructor ``client=`` param — no vnstock, no live calls.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from app.retrieval.adapters.viet_stock import VietStockAdapter
from app.retrieval.fa_sources import (
    CANONICAL_FA_FIELDS,
    FAAggregator,
    FASourceError,
    SourceFinancials,
    SourceOHLCV,
)
from app.retrieval.fa_sources.base import ensure_symbol
from app.retrieval.fa_sources.fireant import FireAntFASource
from app.retrieval.fa_sources.ssi import SSIFASource
from app.retrieval.fa_sources.tcbs import TCBSFASource


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- fixtures: wire payloads -------------------------------------------------

_SSI_BODY = {
    "s": "ok",
    "t": [1700000000 + i * 86400 for i in range(6)],
    "o": [20000.0] * 6,
    "h": [21000.0] * 6,
    "l": [19500.0] * 6,
    "c": [20500.0] * 6,
    "v": [1_000_000.0] * 6,
}

_TCBS_BODY = {
    "data": [
        {"date": "2023-12-31", "PE": 9.0, "PB": 1.5},
        {
            "date": "2024-12-31",
            "PE": 12.5,
            "PB": 2.0,
            "ROE": 15.3,
            "EPS": 5000,
            "marketCap": 3.2e13,
        },
    ]
}

_FIREANT_INCOME = [
    {
        "fiscalDate": "2025-09-30",
        "totalRevenue": 5.2e12,
        "netProfit": 4.5e11,
        "revenueYoy": 12.0,
    }
]
_FIREANT_BALANCE = [
    {
        "fiscalDate": "2025-09-30",
        "totalEquity": 3e12,
        "totalDebt": 1.5e12,
        "totalAssets": 6e12,
    }
]


def _fireant_route(income_status: int = 200, balance_status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if "/income-statement" in request.url.path:
            return httpx.Response(
                income_status, json=[] if income_status != 200 else _FIREANT_INCOME
            )
        if "/balance-sheet" in request.url.path:
            return httpx.Response(
                balance_status, json=[] if balance_status != 200 else _FIREANT_BALANCE
            )
        return httpx.Response(404, json=[])

    return handler


# --- symbol guard (URL-path injection defense) --------------------------------


def test_ensure_symbol_normalizes_and_rejects():
    assert ensure_symbol("hpg") == "HPG"
    assert ensure_symbol(" HPG ") == "HPG"
    assert ensure_symbol("FPT") == "FPT"
    assert ensure_symbol("../etc") is None
    assert ensure_symbol("HPG/GBP") is None
    assert ensure_symbol("HPG?x=1") is None
    assert ensure_symbol("") is None


def test_adapters_reject_unsafe_symbol_without_network():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unsafe symbol must not reach the network")

    async def checks():
        async with _client(handler) as client:
            assert await SSIFASource(client=client).fetch_ohlcv("../etc") is None
            assert await TCBSFASource(client=client).fetch_financials("A/B") is None
            assert await FireAntFASource(token="t", client=client).fetch_financials("A?B") is None

    asyncio.run(checks())


# --- SSI ----------------------------------------------------------------------


class TestSSIFASource:
    async def test_parses_tradingview_payload_without_price_scaling(self):
        async with _client(lambda r: httpx.Response(200, json=_SSI_BODY)) as client:
            src = SSIFASource(client=client)
            out = await src.fetch_ohlcv("HPG")
        assert out is not None
        assert out.source_id == "ssi"
        assert out.closes == [20500.0] * 6  # NO x1000 correction invented
        assert out.last_ts == _SSI_BODY["t"][-1]
        assert out.n == 6
        assert out.source_url.startswith("https://iboard.ssi.com.vn/")

    async def test_no_data_returns_none_not_fault(self):
        async with _client(lambda r: httpx.Response(200, json={"s": "no_data"})) as client:
            out = await SSIFASource(client=client).fetch_ohlcv("XYZ")
        assert out is None

    async def test_short_series_returns_none(self):
        body = {"s": "ok", "t": [1, 2], "c": [10.0, 11.0]}
        async with _client(lambda r: httpx.Response(200, json=body)) as client:
            out = await SSIFASource(client=client).fetch_ohlcv("XYZ")
        assert out is None

    async def test_http_500_raises_upstream_error(self):
        async with _client(lambda r: httpx.Response(500, text="boom")) as client:
            with pytest.raises(FASourceError) as exc:
                await SSIFASource(client=client).fetch_ohlcv("HPG")
        assert exc.value.outcome.value == "upstream_error"

    async def test_bad_json_raises(self):
        async with _client(lambda r: httpx.Response(200, text="not-json")) as client:
            with pytest.raises(FASourceError):
                await SSIFASource(client=client).fetch_ohlcv("HPG")

    async def test_negative_values_raise(self):
        body = {**_SSI_BODY, "c": [-1.0] * 6}
        async with _client(lambda r: httpx.Response(200, json=body)) as client:
            with pytest.raises(FASourceError):
                await SSIFASource(client=client).fetch_ohlcv("HPG")

    async def test_fetch_financials_is_none(self):
        out = await SSIFASource().fetch_financials("HPG")
        assert out is None


# --- TCBS ---------------------------------------------------------------------


class TestTCBSFASource:
    async def test_maps_known_keys_and_latest_record(self):
        async with _client(lambda r: httpx.Response(200, json=_TCBS_BODY)) as client:
            out = await TCBSFASource(client=client).fetch_financials("TCB")
        assert out is not None
        assert out.source_id == "tcbs"
        assert out.fields["pe"] == 12.5  # latest record wins, not 9.0
        assert out.fields["pb"] == 2.0
        assert out.fields["roe_pct"] == 15.3
        assert out.fields["eps_vnd"] == 5000.0
        assert out.fields["market_cap_vnd_bil"] == 32000.0  # 3.2e13 / 1e9
        assert out.as_of == datetime(2024, 12, 31, tzinfo=UTC)

    async def test_unknown_keys_skipped_not_invented(self):
        body = {"data": [{"date": "2024-12-31", "mysteryRatio": 42.0}]}
        async with _client(lambda r: httpx.Response(200, json=body)) as client:
            out = await TCBSFASource(client=client).fetch_financials("TCB")
        assert out is None  # schema drifted entirely -> emit nothing

    async def test_http_error_raises(self):
        async with _client(lambda r: httpx.Response(503, text="x")) as client:
            with pytest.raises(FASourceError):
                await TCBSFASource(client=client).fetch_financials("TCB")

    async def test_fetch_ohlcv_is_none(self):
        out = await TCBSFASource().fetch_ohlcv("TCB")
        assert out is None


# --- FireAnt ------------------------------------------------------------------


class TestFireAntFASource:
    async def test_disabled_without_token(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("network call must not happen without a token")

        async with _client(handler) as client:
            out = await FireAntFASource(token="", client=client).fetch_financials("TCB")
        assert out is None

    async def test_parses_statements_with_bearer_and_computes_de(self):
        seen_auth: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_auth.append(request.headers.get("Authorization", ""))
            return _fireant_route()(request)

        async with _client(handler) as client:
            out = await FireAntFASource(token="tok123", client=client).fetch_financials("TCB")
        assert out is not None
        assert seen_auth and all(a == "Bearer tok123" for a in seen_auth)
        assert out.fields["revenue_vnd_bil"] == 5200.0  # 5.2e12 / 1e9
        assert out.fields["net_profit_vnd_bil"] == 450.0
        assert out.fields["revenue_growth_pct"] == 12.0
        assert out.fields["total_equity_vnd_bil"] == 3000.0
        assert out.fields["debt_to_equity"] == 0.5  # 1.5e12 / 3e12
        assert out.as_of == datetime(2025, 9, 30, tzinfo=UTC)
        assert out.statement_records == 2

    async def test_auth_error_raises_rate_limited(self):
        async with _client(_fireant_route(income_status=401)) as client:
            with pytest.raises(FASourceError) as exc:
                await FireAntFASource(token="tok123", client=client).fetch_financials("TCB")
        assert exc.value.outcome.value == "rate_limited"

    async def test_one_statement_failing_keeps_other_fields(self):
        async with _client(_fireant_route(income_status=500, balance_status=200)) as client:
            out = await FireAntFASource(token="tok123", client=client).fetch_financials("TCB")
        assert out is not None
        assert "total_equity_vnd_bil" in out.fields
        assert "revenue_vnd_bil" not in out.fields

    async def test_both_statements_failing_raises(self):
        async with _client(_fireant_route(income_status=500, balance_status=500)) as client:
            with pytest.raises(FASourceError):
                await FireAntFASource(token="tok123", client=client).fetch_financials("TCB")

    async def test_fetch_ohlcv_is_none(self):
        out = await FireAntFASource(token="x").fetch_ohlcv("TCB")
        assert out is None


# --- aggregator ---------------------------------------------------------------


class _FakeSource:
    def __init__(
        self,
        sid: str,
        priority: float = 1.0,
        financials: SourceFinancials | None = None,
        ohlcv: SourceOHLCV | None = None,
        error: FASourceError | None = None,
        calls: list[str] | None = None,
    ) -> None:
        self.source_id = sid
        self.priority = priority
        self._financials = financials
        self._ohlcv = ohlcv
        self._error = error
        self._calls = calls

    async def fetch_financials(
        self, symbol: str, *, timeout_s: float = 3.5
    ) -> SourceFinancials | None:
        if self._calls is not None:
            self._calls.append(self.source_id)
        if self._error is not None:
            raise self._error
        return self._financials

    async def fetch_ohlcv(self, symbol: str, *, timeout_s: float = 3.5) -> SourceOHLCV | None:
        if self._calls is not None:
            self._calls.append(self.source_id)
        if self._error is not None:
            raise self._error
        return self._ohlcv


def _fin(sid: str, fields: dict[str, float], as_of: datetime | None = None):
    return SourceFinancials(source_id=sid, symbol="HPG", fields=fields, as_of=as_of)


class TestFAAggregator:
    async def test_priority_winner_and_cross_validation(self):
        a = _FakeSource("a", 1.0, _fin("a", {"pe": 10.0, "pb": 2.0}))
        b = _FakeSource("b", 0.5, _fin("b", {"pe": 10.04, "roe_pct": 15.0}))
        out = await FAAggregator([b, a]).fetch_financials("HPG")
        # 0.4% apart -> consensus, higher priority keeps the crown.
        assert out.fields["pe"].source == "a"
        assert out.fields["pe"].validated is True
        assert out.fields["pb"].validated is False
        assert out.fields["roe_pct"].source == "b"
        assert out.sources_used == ["a", "b"]
        assert "revenue_vnd_bil" in out.missing_fields

    async def test_conflict_keeps_priority_and_logs(self, caplog):
        a = _FakeSource("a", 1.0, _fin("a", {"pe": 10.0}))
        b = _FakeSource("b", 0.5, _fin("b", {"pe": 20.0}))
        with caplog.at_level("WARNING", logger="app.retrieval.fa_sources.aggregator"):
            out = await FAAggregator([b, a]).fetch_financials("HPG")
        assert out.fields["pe"].source == "a"
        assert out.fields["pe"].validated is False
        assert any("fa_conflict" in r.message for r in caplog.records)

    async def test_all_sources_down_gives_empty_fields_no_invention(self):
        err = FASourceError("a", "upstream_error", "down")
        a = _FakeSource("a", 1.0, error=err)
        b = _FakeSource("b", 0.5, error=err)
        out = await FAAggregator([a, b]).fetch_financials("HPG")
        assert out.fields == {}
        assert out.missing_fields == list(CANONICAL_FA_FIELDS)
        assert out.sources_used == []

    async def test_breaker_opens_after_three_faults(self):
        calls: list[str] = []
        err = FASourceError("bad", "upstream_error", "down")
        bad = _FakeSource("bad", 1.0, error=err, calls=calls)
        good = _FakeSource("good", 0.5, _fin("good", {"pb": 2.0}))
        agg = FAAggregator([bad, good])
        for _ in range(3):
            await agg.fetch_financials("HPG")
        await agg.fetch_financials("HPG")  # 4th: breaker open -> skipped
        assert calls == ["bad", "bad", "bad"]

    async def test_slow_source_times_out_without_sinking_fanout(self):
        class Slow(_FakeSource):
            async def fetch_financials(self, symbol: str, *, timeout_s: float = 3.5):
                await asyncio.sleep(0.2)
                return self._financials

        slow = Slow("slow", 1.0, _fin("slow", {"pe": 9.0}))
        fast = _FakeSource("fast", 0.5, _fin("fast", {"pb": 2.0}))
        agg = FAAggregator([slow, fast], timeout_s=0.05)
        out = await agg.fetch_financials("HPG")
        assert out.get("pb") == 2.0
        assert out.get("pe") is None

    async def test_ohlcv_first_success_by_priority(self):
        one = _FakeSource("one", 0.9, ohlcv=SourceOHLCV("one", closes=[1, 2, 3, 4, 5]))
        two = _FakeSource("two", 0.5, ohlcv=SourceOHLCV("two", closes=[9, 9, 9, 9, 9]))
        out = await FAAggregator([two, one]).fetch_ohlcv("HPG")
        assert out is not None and out.source_id == "one"

    async def test_ohlcv_falls_through_to_second_source(self):
        err = FASourceError("one", "upstream_error", "down")
        one = _FakeSource("one", 0.9, error=err)
        two = _FakeSource("two", 0.5, ohlcv=SourceOHLCV("two", closes=[9, 9, 9, 9, 9]))
        out = await FAAggregator([one, two]).fetch_ohlcv("HPG")
        assert out is not None and out.source_id == "two"


# --- VietStockAdapter integration ---------------------------------------------


def _multi_route(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if host == "dchart-api.vndirect.com.vn":
        return httpx.Response(500, text="boom")
    if host == "api.simplize.vn":
        return httpx.Response(500, text="boom")
    if host == "iboard.ssi.com.vn":
        return httpx.Response(200, json=_SSI_BODY)
    if host == "api.tcbs.com.vn":
        return httpx.Response(200, json=_TCBS_BODY)
    return httpx.Response(404, json=[])


class TestVietStockAdapterFallback:
    async def test_stock_result_served_by_ssi_and_tcbs_with_provenance(self):
        adapter = VietStockAdapter(client=_client(_multi_route))
        results = await adapter.search("HPG kỹ thuật", max_results=10)

        tech = [r for r in results if "Phân Tích Kỹ Thuật" in r.title]
        assert tech, "OHLCV must come from the SSI fallback"
        assert tech[0].source_name == "SSI"
        assert tech[0].source_url.startswith("https://iboard.ssi.com.vn/")
        assert "Real SSI TradingView EOD" in tech[0].snippet

        fa = [r for r in results if r.source_name.startswith("FA-Aggregator")]
        assert fa, "fundamentals must come from the TCBS fallback"
        assert "tcbs" in fa[0].source_name
        assert fa[0].source_url.startswith("https://api.tcbs.com.vn/")
        assert "P/E: 12.5" in fa[0].snippet
        assert "Nguồn FA đa nguồn" in fa[0].snippet
        assert fa[0].as_of == datetime(2024, 12, 31, tzinfo=UTC)

    async def test_index_result_served_by_ssi_fallback(self):
        adapter = VietStockAdapter(client=_client(_multi_route))
        results = await adapter.search("VNINDEX hôm nay", max_results=10)
        idx = [r for r in results if "Chỉ Số VNINDEX" in r.title]
        assert idx, "index candles must come from the SSI fallback"
        assert idx[0].source_name == "SSI"

    async def test_all_sources_down_returns_empty_results(self):
        def dead_route(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        adapter = VietStockAdapter(client=_client(dead_route))
        results = await adapter.search("HPG", max_results=10)
        assert results == []  # graceful degradation, zero invention
