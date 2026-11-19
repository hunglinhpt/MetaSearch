"""Tests for the Market Intelligence Orchestration Layer (docs/30 Part 2).

Covers peer auto-discovery, the price/technical alert engine, and
news-to-price event-impact computation. All fixtures are synthetic
OHLCV series (test data, not runtime fabrication).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.retrieval.intel import (
    _calc_rsi_14,
    compute_event_impact,
    discover_peers,
    evaluate_alert_conditions,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TICKERS = {
    "VCB": {"company_name": "Ngân hàng TMCP Quân đội", "exchange": "HOSE"},
    "BID": {"company_name": "Ngân hàng TMCP Đầu tư và Phát triển", "exchange": "HOSE"},
    "CTG": {"company_name": "Ngân hàng TMCP Công Thương", "exchange": "HOSE"},
    "FPT": {"company_name": "Công ty Cổ phần FPT", "exchange": "HOSE"},
    "HPG": {"company_name": "Công ty Cổ phần Tập đoàn Hòa Phát", "exchange": "HOSE"},
}


def _make_bars(
    n: int,
    *,
    start_price: float = 100.0,
    drift: float = 0.0,
    last_volume: float = 1_000_000,
    base_volume: float = 1_000_000,
) -> list[dict]:
    """Synthetic daily OHLCV series ending today."""
    bars = []
    price = start_price
    today = datetime.now(UTC).date()
    for i in range(n):
        price = price * (1 + drift)
        volume = base_volume
        if i == n - 1:
            volume = last_volume
        ts = datetime(today.year, today.month, today.day, tzinfo=UTC) - timedelta(days=n - 1 - i)
        bars.append(
            {
                "time": ts.timestamp(),
                "open": price,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "volume": volume,
            }
        )
    return bars


# ---------------------------------------------------------------------------
# Peer discovery
# ---------------------------------------------------------------------------


class TestPeerDiscovery:
    def test_banks_cluster_together(self):
        group = discover_peers(_TICKERS, "VCB", max_peers=5)
        symbols = [p["symbol"] for p in group.peers]
        # All 3 bank peers share "ngân" token — but VCB itself must be excluded
        assert "VCB" not in symbols
        assert set(symbols) == {"BID", "CTG"}
        assert all(p["score"] >= 1 for p in group.peers)

    def test_unknown_target_returns_empty(self):
        group = discover_peers(_TICKERS, "NOPE")
        assert group.target == "NOPE"
        assert group.peers == []

    def test_max_peers_cap(self):
        group = discover_peers(_TICKERS, "VCB", max_peers=1)
        assert len(group.peers) <= 1

    def test_index_symbols_skipped(self):
        tickers = dict(_TICKERS)
        tickers["^VNINDEX"] = {"company_name": "Ngân hàng VN-Index", "exchange": "HOSE"}
        group = discover_peers(tickers, "VCB")
        assert "^VNINDEX" not in [p["symbol"] for p in group.peers]


# ---------------------------------------------------------------------------
# Alert engine
# ---------------------------------------------------------------------------


class TestAlertEngine:
    def test_insufficient_bars_no_alerts(self):
        alerts = evaluate_alert_conditions("VCB", _make_bars(5))
        assert alerts == []

    def test_rsi_overbought_triggers(self):
        # 20 bars rising steadily → RSI high
        bars = _make_bars(20, drift=0.02)
        alerts = evaluate_alert_conditions("FPT", bars)
        conditions = {a.condition for a in alerts}
        assert "RSI_OVERBOUGHT" in conditions

    def test_rsi_oversold_triggers(self):
        bars = _make_bars(20, drift=-0.02)
        alerts = evaluate_alert_conditions("FPT", bars)
        conditions = {a.condition for a in alerts}
        assert "RSI_OVERSOLD" in conditions

    def test_volume_spike_triggers(self):
        bars = _make_bars(20, last_volume=5_000_000, base_volume=1_000_000)
        alerts = evaluate_alert_conditions("FPT", bars)
        conditions = {a.condition for a in alerts}
        assert "VOLUME_SPIKE" in conditions

    def test_neutral_series_triggers_nothing(self):
        # Flat price + flat volume → no alert
        bars = _make_bars(20, drift=0.0, last_volume=1_000_000, base_volume=1_000_000)
        alerts = evaluate_alert_conditions("FPT", bars)
        assert alerts == []

    def test_near_20d_high(self):
        # Rising series ends near its own 20-day high
        bars = _make_bars(25, drift=0.01)
        alerts = evaluate_alert_conditions("FPT", bars)
        conditions = {a.condition for a in alerts}
        assert "NEAR_20D_HIGH" in conditions

    def test_rsi_wilder_math(self):
        # Monotonic up closes → RSI = 100 (avg_loss = 0)
        closes = [float(i) for i in range(1, 16)]
        assert _calc_rsi_14(closes) == 100.0
        # Fewer than 15 closes → None
        assert _calc_rsi_14(closes[:5]) is None


# ---------------------------------------------------------------------------
# Event impact
# ---------------------------------------------------------------------------


class TestEventImpact:
    def test_basic_impact_computed(self):
        # 10 bars ending today, event 5 days ago
        bars = _make_bars(10, start_price=100.0, drift=0.01)
        event_date = (datetime.now(UTC) - timedelta(days=5)).date().isoformat()
        impact = compute_event_impact("FPT", event_date, bars, window_days=3)
        assert impact is not None
        assert impact["ticker"] == "FPT"
        assert impact["return_1d_pct"] is not None
        assert impact["return_3d_pct"] is not None
        assert impact["bars_used"] > 0
        # Sanity: rising series → positive returns
        assert impact["return_1d_pct"] > 0

    def test_invalid_date_returns_none(self):
        bars = _make_bars(10)
        assert compute_event_impact("FPT", "not-a-date", bars) is None

    def test_no_time_data_returns_none(self):
        bars = [{"open": 1, "close": 1, "volume": 1} for _ in range(5)]
        assert compute_event_impact("FPT", "2026-11-01", bars) is None

    def test_event_after_last_bar_returns_none(self):
        # No forward candles after a future event → None (never fabricate)
        bars = _make_bars(10, start_price=100.0, drift=0.005)
        event_date = (datetime.now(UTC) + timedelta(days=30)).date().isoformat()
        assert compute_event_impact("FPT", event_date, bars, window_days=5) is None

    def test_missing_candles_returns_none(self):
        # Only 2 bars — below the 3-bar minimum
        bars = _make_bars(2)
        event_date = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
        assert compute_event_impact("FPT", event_date, bars) is None
