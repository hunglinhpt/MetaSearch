"""Direct tests for market.py `_fetch_alert_candles` conversion & fallback
branches (docs/30 Part 2): FA-aggregator fallback, schema-drift refusal,
non-numeric candle refusal — the alert surface never fabricates a candle."""

from __future__ import annotations

import pytest

from app.api.v1 import market
from app.retrieval.fa_sources.base import SourceOHLCV


def _src(n: int = 30) -> SourceOHLCV:
    return SourceOHLCV(
        source_id="ssi",
        opens=[1.0] * n,
        highs=[2.0] * n,
        lows=[0.5] * n,
        closes=[1.5] * n,
        volumes=[10.0] * n,
        times=[1_760_000_000 + i * 86400 for i in range(n)],
        last_ts=1_760_000_000 + (n - 1) * 86400,
        source_url="https://ssi.example/vcb",
    )


@pytest.mark.asyncio
async def test_alert_candles_vndirect_exception_falls_back_to_fa(monkeypatch):
    async def broken(*a, **kw):
        raise RuntimeError("vndirect down")

    monkeypatch.setattr(market, "fetch_vndirect_ohlcv_series", broken)

    class Agg:
        async def fetch_ohlcv(self, symbol, timeout_s=5.0, **kw):
            return _src()

    monkeypatch.setattr(market.deps, "get_fa_aggregator", lambda: Agg())
    candles, source_name = await market._fetch_alert_candles("VCB", 30, 2.0)
    assert source_name == "SSI"
    assert len(candles) == 30
    assert candles[-1].close == 1.5


@pytest.mark.asyncio
async def test_alert_candles_fallback_exception_returns_none(monkeypatch):
    async def broken(*a, **kw):
        raise RuntimeError("vndirect down")

    monkeypatch.setattr(market, "fetch_vndirect_ohlcv_series", broken)

    class DeadAgg:
        async def fetch_ohlcv(self, symbol, timeout_s=5.0, **kw):
            raise RuntimeError("ssi down too")

    monkeypatch.setattr(market.deps, "get_fa_aggregator", lambda: DeadAgg())
    assert await market._fetch_alert_candles("VCB", 30, 2.0) is None


@pytest.mark.asyncio
async def test_alert_candles_mismatched_arrays_refused(monkeypatch):
    async def drift(*a, **kw):
        series = {
            "times": [1, 2, 3],
            "opens": [1.0, 1.0],
            "highs": [2.0, 2.0, 2.0],
            "lows": [0.5, 0.5, 0.5],
            "closes": [1.5, 1.5, 1.5],
            "volumes": [10.0, 10.0, 10.0],
            "last_ts": 3,
        }
        return series

    monkeypatch.setattr(market, "fetch_vndirect_ohlcv_series", drift)
    assert await market._fetch_alert_candles("VCB", 30, 2.0) is None


@pytest.mark.asyncio
async def test_alert_candles_non_numeric_closes_refused(monkeypatch):
    async def bad(*a, **kw):
        return {
            "times": [1, 2],
            "opens": [1.0, "x"],  # non-numeric -> OhlcvCandle raises -> None
            "highs": [2.0, 2.0],
            "lows": [0.5, 0.5],
            "closes": [1.5, 1.5],
            "volumes": [10.0, 10.0],
            "last_ts": 2,
        }

    monkeypatch.setattr(market, "fetch_vndirect_ohlcv_series", bad)
    assert await market._fetch_alert_candles("VCB", 30, 2.0) is None


@pytest.mark.asyncio
async def test_alert_candles_truncated_to_days(monkeypatch):
    async def long_series(*a, **kw):
        n = 120
        return {
            "times": [1_760_000_000 + i * 86400 for i in range(n)],
            "opens": [1.0] * n,
            "highs": [2.0] * n,
            "lows": [0.5] * n,
            "closes": [1.5] * n,
            "volumes": [10.0] * n,
            "last_ts": 1_760_000_000 + (n - 1) * 86400,
        }

    monkeypatch.setattr(market, "fetch_vndirect_ohlcv_series", long_series)
    candles, _source = await market._fetch_alert_candles("VCB", 30, 2.0)
    assert len(candles) == 30
    assert candles[-1].time > candles[0].time
