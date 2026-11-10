"""Multi-source FA (docs/15): direct adapters + aggregator cross-validation.

Every network touch is faked with httpx.MockTransport injected via the
constructor ``client=`` param — no vnstock, no live calls.
"""

from __future__ import annotations

import asyncio
import json
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


async def test_adapters_reject_unsafe_symbol_without_network():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unsafe symbol must not reach the network")

    async with _client(handler) as client:
        assert await SSIFASource(client=client).fetch_ohlcv("../etc") is None
        assert await TCBSFASource(client=client).fetch_financials("A/B") is None
        assert await FireAntFASource(token="t", client=client).fetch_financials("A?B") is None


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
        can_financials: bool = True,
        can_ohlcv: bool = True,
    ) -> None:
        self.source_id = sid
        self.priority = priority
        self.can_financials = can_financials
        self.can_ohlcv = can_ohlcv
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
        assert "Nguồn FA: tcbs (đơn nguồn)" in fa[0].snippet
        assert "chưa đối chiếu chéo" in fa[0].snippet
        assert "mốc dữ liệu: tcbs 2024-12-31" in fa[0].snippet
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


# --- audit round-2 hardening: capability fan-out, half-open, units, dates -----


_VNDIRECT_BODY = {
    "s": "ok",
    "t": _SSI_BODY["t"],
    "o": [21.2] * 6,
    "h": [21.4] * 6,
    "l": [20.8] * 6,
    "c": [21.25] * 6,  # scaled feed: x1000 -> 21,250 VND native (probed 2026-11-11)
    "v": [1_000_000.0] * 6,
}

_SIMPLIZE_BODY = {
    "status": 200,
    "data": {
        "marketCap": 3.2e13,
        "peRatio": 12.5,
        "pbRatio": 2.0,
        "epsRatio": 5000,
        "roe": 15.3,
        "roa": 7.0,
        "priceClose": 21250.0,
        "nameVi": "Công ty Cổ phần Hòa Phát",
        "stockExchange": "HSX",
        "industryActivity": "Thép",
        "analysisUpdated": "01/01/2025",
    },
}


class TestCapabilityFanout:
    async def test_stub_legs_never_probed_nor_recorded(self):
        fin_calls: list[str] = []
        ohlc_calls: list[str] = []
        fin_only = _FakeSource(
            "fin", 1.0, _fin("fin", {"pe": 10.0}), calls=fin_calls, can_ohlcv=False
        )
        ohlc_only = _FakeSource(
            "ohlc",
            0.5,
            ohlcv=SourceOHLCV("ohlc", closes=[1, 2, 3, 4, 5]),
            calls=ohlc_calls,
            can_financials=False,
        )
        agg = FAAggregator([fin_only, ohlc_only])

        await agg.fetch_financials("HPG")
        assert ohlc_calls == []  # OHLCV-only stub never probed for financials
        fin_calls_after_financials = len(fin_calls)

        await agg.fetch_ohlcv("HPG")
        assert len(fin_calls) == fin_calls_after_financials  # no extra probe

    async def test_vacuous_empty_does_not_reset_failure_streak(self):
        calls: list[str] = []
        err = FASourceError("fin", "upstream_error", "down")
        broken_fin = _FakeSource("fin", 1.0, error=err, calls=calls, can_ohlcv=False)
        agg = FAAggregator([broken_fin])
        for _ in range(3):
            await agg.fetch_financials("HPG")
        breaker = agg._registry.get("fa:fin")
        assert breaker.state == "open"
        failures_before = breaker.consecutive_failures

        await agg.fetch_ohlcv("HPG")  # no OHLCV-capable source at all
        assert calls == ["fin", "fin", "fin"]  # no extra probe
        breaker2 = agg._registry.get("fa:fin")
        assert breaker2.state == "open"  # NOT healed by a leg it cannot serve
        assert breaker2.consecutive_failures == failures_before


class TestBreakerHalfOpen:
    async def test_exactly_one_probe_after_cooldown_then_close(self):
        err = FASourceError("a", "upstream_error", "down")
        good = _fin("a", {"pe": 10.0})
        src = _FakeSource("a", 1.0, good, error=err)
        agg = FAAggregator([src])
        for _ in range(3):
            await agg.fetch_financials("HPG")
        bid = "fa:a"
        breaker = agg._registry.get(bid)
        assert breaker.state == "open"

        # Before cooldown: still open, no probe.
        assert agg._registry.allow(bid, now=breaker.retry_at - 1) is False
        # After cooldown: exactly ONE trial admitted.
        assert agg._registry.allow(bid, now=breaker.retry_at + 1) is True
        assert agg._registry.allow(bid, now=breaker.retry_at + 1) is False
        # Simulate the trial completing (deadline/stuck-probe release), then a
        # real fetch probes half-open and a success closes the breaker.
        breaker.half_open_in_flight = False
        src._error = None
        await agg.fetch_financials("HPG")
        assert agg._registry.get(bid).state == "closed"

    async def test_all_breakers_open_returns_empty_without_fail_open(self):
        err = FASourceError("a", "upstream_error", "down")
        agg = FAAggregator([_FakeSource("a", 1.0, error=err)])
        for _ in range(3):
            await agg.fetch_financials("HPG")
        out = await agg.fetch_financials("HPG")  # cooldown not elapsed
        assert out.fields == {}
        assert out.missing_fields == list(CANONICAL_FA_FIELDS)


class TestConsensusThreeSources:
    async def test_two_agreeing_challengers_validate_winner(self):
        a = _FakeSource("a", 1.0, _fin("a", {"pe": 10.0}))
        b = _FakeSource("b", 0.5, _fin("b", {"pe": 10.04}))
        c = _FakeSource("c", 0.4, _fin("c", {"pe": 10.04}))
        out = await FAAggregator([c, b, a]).fetch_financials("HPG")
        assert out.fields["pe"].source == "a"
        assert out.fields["pe"].validated is True

    async def test_majority_against_winner_is_logged(self, caplog):
        a = _FakeSource("a", 1.0, _fin("a", {"pe": 10.0}))
        b = _FakeSource("b", 0.5, _fin("b", {"pe": 20.0}))
        c = _FakeSource("c", 0.4, _fin("c", {"pe": 20.0}))
        with caplog.at_level("WARNING", logger="app.retrieval.fa_sources.aggregator"):
            out = await FAAggregator([c, b, a]).fetch_financials("HPG")
        assert out.fields["pe"].source == "a"  # priority rule holds
        assert any("fa_conflict_majority" in r.message for r in caplog.records)

    async def test_conflict_log_carries_symbol_field_values_sources(self, caplog):
        a = _FakeSource("a", 1.0, _fin("a", {"pe": 10.0}))
        b = _FakeSource("b", 0.5, _fin("b", {"pe": 20.0}))
        with caplog.at_level("WARNING", logger="app.retrieval.fa_sources.aggregator"):
            await FAAggregator([b, a]).fetch_financials("HPG")
        line = next(r for r in caplog.records if r.getMessage().startswith("fa_conflict:"))
        msg = line.getMessage()
        assert "HPG" in msg and "pe" in msg
        assert "10.0" in msg and "20.0" in msg
        assert "a" in msg and "b" in msg

    async def test_duplicate_source_ids_raise(self):
        with pytest.raises(ValueError):
            FAAggregator([_FakeSource("x", 1.0), _FakeSource("x", 0.5)])


class TestTCBSUnitGates:
    async def test_implausible_ratio_and_caps_dropped_with_warning(self, caplog):
        body = {
            "data": [
                {
                    "date": "2024-12-31",
                    "PE": 12.5,
                    "ROE": 300.0,  # beyond ±200% — ratio/percent confusion
                    "marketCap": 2.0e9,  # -> 2.0 tỷ đ, below the 5 tỷ band
                    "EPS": 5.0e9,  # beyond ±1,000,000 VND
                }
            ]
        }
        with caplog.at_level("WARNING", logger="app.retrieval.fa_sources.tcbs"):
            async with _client(lambda r: httpx.Response(200, json=body)) as client:
                out = await TCBSFASource(client=client).fetch_financials("TCB")
        assert out is not None
        assert "roe_pct" not in out.fields
        assert "market_cap_vnd_bil" not in out.fields
        assert "eps_vnd" not in out.fields
        assert out.fields["pe"] == 12.5
        assert any("fa_suspect_unit" in r.message for r in caplog.records)

    async def test_market_cap_missing_is_safe(self):
        body = {"data": [{"date": "2024-12-31", "PE": 12.5}]}
        async with _client(lambda r: httpx.Response(200, json=body)) as client:
            out = await TCBSFASource(client=client).fetch_financials("TCB")
        assert out is not None
        assert "market_cap_vnd_bil" not in out.fields


class TestSSIDataIntegrity:
    async def test_nan_candles_rejected(self):
        body = {**_SSI_BODY, "c": [float("nan")] * 6}
        # httpx's json= rejects NaN; emit it the way a real upstream could.
        payload = json.dumps(body).encode("utf-8")  # stdlib emits bare NaN
        async with _client(
            lambda r: httpx.Response(
                200, content=payload, headers={"content-type": "application/json"}
            )
        ) as client:
            with pytest.raises(FASourceError) as exc:
                await SSIFASource(client=client).fetch_ohlcv("HPG")
        assert "non_finite" in exc.value.detail

    async def test_missing_ohlc_arrays_refused_not_filled(self):
        body = {"s": "ok", "t": _SSI_BODY["t"], "c": _SSI_BODY["c"]}  # no o/h/l/v
        async with _client(lambda r: httpx.Response(200, json=body)) as client:
            out = await SSIFASource(client=client).fetch_ohlcv("HPG")
        assert out is None  # schema drift -> honest refusal, no close-for-open fill

    async def test_oversized_payload_rejected(self):
        big = b"x" * (5 * 1024 * 1024 + 1)
        async with _client(lambda r: httpx.Response(200, content=big)) as client:
            with pytest.raises(FASourceError) as exc:
                await SSIFASource(client=client).fetch_ohlcv("HPG")
        assert "payload_too_large" in exc.value.detail


class TestFireAntIntegrity:
    async def test_partial_fault_flagged_and_logged(self, caplog):
        client = _client(_fireant_route(income_status=500, balance_status=200))
        async with client:
            agg = FAAggregator([FireAntFASource(token="tok", client=client)])
            # The aggregator logs fa_source_degraded once for the merged payload.
            with caplog.at_level("WARNING", logger="app.retrieval.fa_sources.aggregator"):
                out = await agg.fetch_financials("TCB")
        assert out.fields  # the healthy statement still serves
        assert out.missing_fields  # the dead statement's fields are missing
        assert any("fa_source_degraded" in r.getMessage() for r in caplog.records)

    async def test_published_at_split_from_period_end(self):
        income = [
            {
                "fiscalDate": "2025-09-30",
                "reportDate": "2025-11-05",
                "totalRevenue": 5.2e12,
            }
        ]
        balance = [{"fiscalDate": "2025-09-30", "totalEquity": 3e12, "totalDebt": 1.5e12}]

        def handler(request: httpx.Request) -> httpx.Response:
            if "/income-statement" in request.url.path:
                return httpx.Response(200, json=income)
            if "/balance-sheet" in request.url.path:
                return httpx.Response(200, json=balance)
            return httpx.Response(404, json=[])

        async with _client(handler) as client:
            out = await FireAntFASource(token="tok", client=client).fetch_financials("TCB")
        assert out is not None
        assert out.as_of == datetime(2025, 9, 30, tzinfo=UTC)  # period end
        assert out.published_at == datetime(2025, 11, 5, tzinfo=UTC)  # publish side


class TestVietStockMixedPrimaries:
    async def test_vndirect_healthy_simplize_down_falls_back_to_tcbs(self):
        def route(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            if host == "dchart-api.vndirect.com.vn":
                return httpx.Response(200, json=_VNDIRECT_BODY)
            if host == "api.simplize.vn":
                return httpx.Response(500, text="boom")
            if host == "api.tcbs.com.vn":
                return httpx.Response(200, json=_TCBS_BODY)
            return httpx.Response(404, json=[])

        adapter = VietStockAdapter(client=_client(route))
        results = await adapter.search("HPG kỹ thuật", max_results=10)
        tech = [r for r in results if "Phân Tích Kỹ Thuật" in r.title]
        assert tech and tech[0].source_name == "VNDIRECT"
        fa = [r for r in results if r.source_name.startswith("FA-Aggregator")]
        assert fa and "tcbs" in fa[0].source_name

    async def test_vndirect_down_simplize_healthy_keeps_simplize_provenance(self):
        def route(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            if host == "dchart-api.vndirect.com.vn":
                return httpx.Response(500, text="boom")
            if host == "api.simplize.vn":
                return httpx.Response(200, json=_SIMPLIZE_BODY)
            if host == "iboard.ssi.com.vn":
                return httpx.Response(200, json=_SSI_BODY)
            return httpx.Response(404, json=[])

        adapter = VietStockAdapter(client=_client(route))
        results = await adapter.search("HPG kỹ thuật", max_results=10)
        tech = [r for r in results if "Phân Tích Kỹ Thuật" in r.title]
        assert tech and tech[0].source_name == "SSI"  # candle fallback
        fa = [r for r in results if r.source_name == "Simplize"]
        assert fa and "simplize.vn" in fa[0].source_url  # FA primary untouched

    async def test_both_primaries_healthy_aggregator_never_invoked(self):
        def route(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            if host == "dchart-api.vndirect.com.vn":
                return httpx.Response(200, json=_VNDIRECT_BODY)
            if host == "api.simplize.vn":
                return httpx.Response(200, json=_SIMPLIZE_BODY)
            raise AssertionError("fallback sources must not be touched")

        adapter = VietStockAdapter(client=_client(route))
        results = await adapter.search("HPG kỹ thuật", max_results=10)
        names = {r.source_name for r in results}
        assert "VNDIRECT" in names and "Simplize" in names
        assert adapter._fa_agg is None  # lazy fallback never built

    async def test_index_all_sources_down_returns_empty(self):
        def dead_route(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        adapter = VietStockAdapter(client=_client(dead_route))
        results = await adapter.search("VNINDEX hôm nay", max_results=10)
        assert results == []  # graceful, no crash on the index branch
