"""Market Intelligence endpoints (docs/30 Part 2): /v1/market/peers and
/v1/market/alerts. All network touches faked — no live calls.
"""

from __future__ import annotations

from app.api import deps
from app.api.v1 import market
from app.retrieval.fa_sources.base import SourceOHLCV


def _series(n: int = 30, base_ts: int = 1_760_000_000) -> dict:
    return {
        "times": [base_ts + i * 86400 for i in range(n)],
        "opens": [100.0 + i for i in range(n)],
        "highs": [101.0 + i for i in range(n)],
        "lows": [99.0 + i for i in range(n)],
        "closes": [100.5 + i for i in range(n)],
        "volumes": [1000.0 * (i + 1) for i in range(n)],
        "last_ts": base_ts + (n - 1) * 86400,
    }


# --- GET /v1/market/peers -------------------------------------------------------


def test_peers_returns_same_exchange_matches(client):
    resp = client.get("/v1/market/peers", params={"symbol": "VCB"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["symbol"] == "VCB"
    # vn_tickers registry is real: VCB (bank) should surface other banks
    symbols = [p["symbol"] for p in body["peers"]]
    assert "VCB" not in symbols
    assert all(len(p["shared_context"]) > 0 for p in body["peers"])


def test_peers_invalid_symbol_400(client):
    resp = client.get("/v1/market/peers", params={"symbol": "bad;symbol"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"


def test_peers_unknown_symbol_returns_empty(client):
    # A structurally-valid but unknown symbol → empty peers, not an error
    resp = client.get("/v1/market/peers", params={"symbol": "ZZZZZ"})
    assert resp.status_code == 200
    assert resp.json()["peers"] == []


# --- POST /v1/market/alerts ------------------------------------------------------


def test_alerts_triggered_on_rising_series(client, monkeypatch):
    async def fake_series(symbol, *, days, timeout_s, **kw):
        return _series(40)

    monkeypatch.setattr(market, "fetch_vndirect_ohlcv_series", fake_series)
    resp = client.post("/v1/market/alerts", json={"symbols": ["HPG"], "days": 40})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["latency_ms"] >= 0
    r = body["results"][0]
    assert r["symbol"] == "HPG"
    assert r["source_name"] == "VNDIRECT"
    assert r["as_of"].startswith("20")
    # Steadily rising series: 20-day-high proximity should fire
    conditions = {a["condition"] for a in r["alerts"]}
    assert "NEAR_20D_CLOSE_HIGH" in conditions
    assert all(a["triggered"] for a in r["alerts"])


def test_alerts_no_data_empty_alerts(client, monkeypatch):
    async def none_series(symbol, *, days, timeout_s, **kw):
        return None

    async def no_fallback(symbol, timeout_s, **kw):
        return None

    monkeypatch.setattr(market, "fetch_vndirect_ohlcv_series", none_series)

    class DeadAgg:
        async def fetch_ohlcv(self, symbol, timeout_s=5.0, **kw):
            return None

    monkeypatch.setattr(deps, "get_fa_aggregator", lambda: DeadAgg())
    resp = client.post("/v1/market/alerts", json={"symbols": ["VCB"]})
    assert resp.status_code == 200, resp.text
    r = resp.json()["results"][0]
    assert r["symbol"] == "VCB"
    assert r["alerts"] == []
    # provenance honesty: nothing fetched → no source claimed
    # (response_model_exclude_none strips the None field from the payload)
    assert "source_name" not in r
    assert "as_of" not in r


def test_alerts_fallback_to_ssi(client, monkeypatch):
    async def none_series(symbol, *, days, timeout_s, **kw):
        return None

    src = SourceOHLCV(
        source_id="ssi",
        opens=[1.0] * 30,
        highs=[2.0] * 30,
        lows=[0.5] * 30,
        closes=[1.5] * 30,
        volumes=[10.0] * 30,
        times=[1_760_000_000 + i * 86400 for i in range(30)],
        last_ts=1_760_000_000 + 29 * 86400,
        source_url="https://ssi.example/vcb",
    )

    async def fake_agg(symbol, timeout_s=5.0, **kw):
        return src

    monkeypatch.setattr(market, "fetch_vndirect_ohlcv_series", none_series)

    class Agg:
        async def fetch_ohlcv(self, symbol, timeout_s=5.0, **kw):
            return fake_agg(symbol, timeout_s) if False else src

    monkeypatch.setattr(deps, "get_fa_aggregator", lambda: Agg())
    resp = client.post("/v1/market/alerts", json={"symbols": ["VCB"]})
    assert resp.status_code == 200, resp.text
    r = resp.json()["results"][0]
    assert r["source_name"] == "SSI"  # provenance: never label SSI data as VNDIRECT


def test_alerts_invalid_symbol_400(client):
    resp = client.post("/v1/market/alerts", json={"symbols": ["VCB;DROP"]})
    assert resp.status_code == 400


def test_alerts_cap_five_symbols(client, monkeypatch):
    """Schema caps symbols at 5 (W5 Rule 2); the project error contract maps
    validation failures to 400 invalid_request."""
    resp = client.post("/v1/market/alerts", json={"symbols": ["A", "B", "C", "D", "E", "F"]})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"


def test_alerts_multi_symbol_bounded(client, monkeypatch):
    async def fake_series(symbol, *, days, timeout_s, **kw):
        return _series(40)

    monkeypatch.setattr(market, "fetch_vndirect_ohlcv_series", fake_series)
    resp = client.post("/v1/market/alerts", json={"symbols": ["HPG", "VCB"]})
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert {r["symbol"] for r in results} == {"HPG", "VCB"}
