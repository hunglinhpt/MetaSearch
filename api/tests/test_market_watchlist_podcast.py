"""v2.3.0 waves B/C/D (docs/18): market compare+ohlcv, watchlist+alerts, podcast.

All network/DB/LLM touches are faked: FA aggregator and VNDIRECT are
monkeypatched, the watchlist uses an in-memory SQLite DB, the podcast uses a
fake VoiceStudio client and a tmp data dir. No live calls.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api import deps
from app.api.v1 import market, watchlist
from app.config import get_settings
from app.retrieval.fa_sources.aggregator import AggregatedFinancials, FieldValue
from app.retrieval.fa_sources.base import SourceOHLCV
from app.retrieval.models import ScoredResult
from app.tts import briefing

# --- fakes -------------------------------------------------------------------


class FakeAggregator:
    def __init__(self, financials=None, ohlcv=None):
        self._financials = financials or {}
        self._ohlcv = ohlcv
        self.calls: list[str] = []

    async def fetch_financials(self, symbol, *, timeout_s=3.5):
        from app.retrieval.fa_sources.base import CANONICAL_FA_FIELDS

        self.calls.append(f"fin:{symbol}")
        agg = self._financials.get(symbol)
        if agg is None:
            # Mirror the real merge: no source answered -> every canonical
            # field is listed missing (nothing invented).
            agg = AggregatedFinancials(symbol=symbol, missing_fields=list(CANONICAL_FA_FIELDS))
        return agg

    async def fetch_ohlcv(self, symbol, *, timeout_s=3.5):
        self.calls.append(f"ohlcv:{symbol}")
        return self._ohlcv


def _agg_with_pe(symbol: str, pe: float, source: str = "tcbs") -> AggregatedFinancials:
    agg = AggregatedFinancials(symbol=symbol)
    agg.fields["pe"] = FieldValue(
        value=pe, source=source, as_of=datetime(2026, 9, 30, tzinfo=UTC), validated=True
    )
    agg.missing_fields = [f for f in agg.missing_fields if f != "pe"]
    agg.sources_used = [source]
    agg.source_urls = [f"https://example.com/{symbol}"]
    agg.as_of = datetime(2026, 9, 30, tzinfo=UTC)
    return agg


def _result(url: str, title: str = "t", days_ago: int = 0) -> ScoredResult:
    return ScoredResult(
        url=url,
        canonical_url=url,
        title=title,
        snippet="s",
        domain="example.com",
        published_at=datetime.now(UTC) - timedelta(days=days_ago),
    )


class FakePipeline:
    def __init__(self, results=None, fail_for: set[str] | None = None):
        self._results = results if results is not None else []
        self._fail_for = fail_for or set()
        self.queries: list[str] = []

    async def run(self, params, on_event=None):
        self.queries.append(params.query)
        if any(f in params.query for f in self._fail_for):
            raise RuntimeError("engine down")

        from app.orchestration.pipeline import SearchOutcome

        return SearchOutcome(
            query_id=uuid.uuid4(),
            results=list(self._results),
            statuses=[],
            latency_ms=1,
        )


class FakeTtsClient:
    def __init__(self, enabled: bool = True, audio: bytes = b"ID3-fake-mp3"):
        self._enabled = enabled
        self._audio = audio
        self.calls: list[str] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def synthesize(self, text, *, voice=None, model=None, response_format="mp3"):
        self.calls.append(text)
        return self._audio, "audio/mpeg"


# --- market: fa/compare --------------------------------------------------------


def test_fa_compare_happy_path(client, monkeypatch):
    agg = FakeAggregator(
        financials={"VCB": _agg_with_pe("VCB", 9.5), "FPT": _agg_with_pe("FPT", 18.2)}
    )
    monkeypatch.setattr(deps, "get_fa_aggregator", lambda: agg)
    resp = client.post("/v1/fa/compare", json={"symbols": ["vcb", "FPT"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [r["symbol"] for r in body["results"]] == ["VCB", "FPT"]
    vcb = body["results"][0]
    assert vcb["fields"]["pe"]["value"] == 9.5
    assert vcb["fields"]["pe"]["validated"] is True
    assert vcb["fields"]["pe"]["source"] == "tcbs"
    assert vcb["fields"]["pe"]["as_of"].startswith("2026-09-30")
    assert vcb["exchange"]  # from DAT-06 registry
    assert "pe" not in vcb["missing_fields"]
    assert agg.calls == ["fin:VCB", "fin:FPT"]


def test_fa_compare_missing_fields_not_invented(client, monkeypatch):
    monkeypatch.setattr(deps, "get_fa_aggregator", lambda: FakeAggregator())
    resp = client.post("/v1/fa/compare", json={"symbols": ["VCB"]})
    assert resp.status_code == 200
    r = resp.json()["results"][0]
    assert r["fields"] == {}
    assert r["missing_fields"]  # everything listed as missing
    assert r["sources_used"] == []


def test_fa_compare_cap_and_symbol_guard(client, monkeypatch):
    monkeypatch.setattr(deps, "get_fa_aggregator", lambda: FakeAggregator())
    resp = client.post(
        "/v1/fa/compare", json={"symbols": ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"
    resp = client.post("/v1/fa/compare", json={"symbols": ["VCB;DROP"]})
    assert resp.status_code == 400
    resp = client.post("/v1/fa/compare", json={"symbols": []})
    assert resp.status_code in (400, 422)  # rejected either by schema or by the guard


# --- market: ohlcv -------------------------------------------------------------


def _series(n: int = 10, base_ts: int = 1_760_000_000) -> dict:
    return {
        "times": [base_ts + i * 86400 for i in range(n)],
        "opens": [100.0 + i for i in range(n)],
        "highs": [101.0 + i for i in range(n)],
        "lows": [99.0 + i for i in range(n)],
        "closes": [100.5 + i for i in range(n)],
        "volumes": [1000.0 * (i + 1) for i in range(n)],
        "last_ts": base_ts + (n - 1) * 86400,
    }


def test_ohlcv_vndirect_primary(client, monkeypatch):
    async def fake_series(symbol, *, days, timeout_s, **kw):
        return _series(40)

    monkeypatch.setattr(market, "fetch_vndirect_ohlcv_series", fake_series)
    resp = client.post("/v1/market/ohlcv", json={"symbol": "hpg", "days": 30})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["symbol"] == "HPG"
    assert body["source_name"] == "VNDIRECT"
    assert len(body["candles"]) == 30  # trimmed to requested window
    c = body["candles"][0]
    assert set(c) == {"time", "open", "high", "low", "close", "volume"}
    assert body["as_of"].startswith("20")  # ISO from last candle ts


def test_ohlcv_fallback_to_ssi(client, monkeypatch):
    async def none_series(symbol, *, days, timeout_s, **kw):
        return None

    src = SourceOHLCV(
        source_id="ssi",
        opens=[1.0] * 6,
        highs=[2.0] * 6,
        lows=[0.5] * 6,
        closes=[1.5] * 6,
        volumes=[10.0] * 6,
        times=[1_760_000_000 + i * 86400 for i in range(6)],
        last_ts=1_760_000_000 + 5 * 86400,
        source_url="https://ssi.example/hpg",
    )
    monkeypatch.setattr(market, "fetch_vndirect_ohlcv_series", none_series)
    monkeypatch.setattr(deps, "get_fa_aggregator", lambda: FakeAggregator(ohlcv=src))
    resp = client.post("/v1/market/ohlcv", json={"symbol": "VCB", "days": 30})
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_name"] == "SSI"
    assert body["source_url"] == "https://ssi.example/hpg"
    assert len(body["candles"]) == 6


def test_ohlcv_all_sources_down_503(client, monkeypatch):
    async def none_series(symbol, *, days, timeout_s, **kw):
        return None

    monkeypatch.setattr(market, "fetch_vndirect_ohlcv_series", none_series)
    monkeypatch.setattr(deps, "get_fa_aggregator", lambda: FakeAggregator(ohlcv=None))
    resp = client.post("/v1/market/ohlcv", json={"symbol": "VCB"})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "no_results"


def test_ohlcv_symbol_guard_and_days_clamp(client, monkeypatch):
    seen: dict[str, int] = {}

    async def fake_series(symbol, *, days, timeout_s, **kw):
        seen["days"] = days
        return _series(3)

    monkeypatch.setattr(market, "fetch_vndirect_ohlcv_series", fake_series)
    resp = client.post("/v1/market/ohlcv", json={"symbol": "bad;symbol"})
    assert resp.status_code == 400
    resp = client.post("/v1/market/ohlcv", json={"symbol": "VCB", "days": 99999})
    assert resp.status_code == 200
    assert seen["days"] == get_settings().market_ohlcv_max_days
    resp = client.post("/v1/market/ohlcv", json={"symbol": "VCB", "days": 1})
    assert resp.status_code == 200
    assert seen["days"] == get_settings().market_ohlcv_min_days


# --- watchlist ------------------------------------------------------------------


@pytest.fixture
def mem_db(monkeypatch):
    """In-memory SQLite shared by the session factory AND _ensure_table, so
    watchlist tests exercise the real DB path (table created via run_sync in
    the app's own loop on first use)."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(deps, "get_session_factory", lambda: factory)
    monkeypatch.setattr("app.db.session.get_engine", lambda: engine)
    monkeypatch.setattr(watchlist, "_table_ready", False)
    yield engine


def _use_owner(monkeypatch, owner: str):
    monkeypatch.setattr(watchlist, "_owner", lambda: owner)


def test_watchlist_crud_tenancy(client, monkeypatch, mem_db):
    _use_owner(monkeypatch, "keyA")
    resp = client.post("/v1/watchlist", json={"query": "VCB", "label": "Ngân hàng"})
    assert resp.status_code == 201, resp.text
    item = resp.json()
    assert item["query"] == "VCB" and item["label"] == "Ngân hàng"
    # duplicate -> 409
    resp = client.post("/v1/watchlist", json={"query": " VCB  "})
    assert resp.status_code == 409
    # list as A sees it
    assert [i["id"] for i in client.get("/v1/watchlist").json()["items"]] == [item["id"]]
    # key B sees nothing and cannot delete A's item (404, no existence leak)
    _use_owner(monkeypatch, "keyB")
    assert client.get("/v1/watchlist").json()["items"] == []
    resp = client.delete(f"/v1/watchlist/{item['id']}")
    assert resp.status_code == 404
    # key A deletes
    _use_owner(monkeypatch, "keyA")
    assert client.delete(f"/v1/watchlist/{item['id']}").status_code == 200
    assert client.get("/v1/watchlist").json()["items"] == []
    # invalid id -> 400
    assert client.delete("/v1/watchlist/not-a-uuid").status_code == 400


def test_watchlist_cap(client, monkeypatch, mem_db):
    _use_owner(monkeypatch, "keyC")
    settings = get_settings()
    for i in range(settings.watchlist_max_items):
        assert client.post("/v1/watchlist", json={"query": f"q{i}"}).status_code == 201
    resp = client.post("/v1/watchlist", json={"query": "one-too-many"})
    assert resp.status_code == 400


def test_watchlist_alerts_baseline_then_new(client, monkeypatch, mem_db):
    _use_owner(monkeypatch, "keyD")
    client.post("/v1/watchlist", json={"query": "FPT tin tức"})
    results1 = [_result("https://a.com/1", "tin 1"), _result("https://a.com/2", "tin 2")]
    pipe = FakePipeline(results=results1)
    monkeypatch.setattr(deps, "get_search_pipeline", lambda: pipe)

    # First check: baseline only, no "new" flood.
    resp = client.get("/v1/watchlist/alerts")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_new"] == 0
    assert body["items"][0]["baseline_set"] is True
    assert body["items"][0]["new_items"] == []

    # Second check with one new URL: exactly that one is reported.
    pipe._results = [_result("https://a.com/2", "tin 2"), _result("https://a.com/3", "tin 3 mới")]
    body = client.get("/v1/watchlist/alerts").json()
    assert body["total_new"] == 1
    new = body["items"][0]["new_items"]
    assert len(new) == 1 and new[0]["url"] == "https://a.com/3"
    assert new[0]["title"] == "tin 3 mới"
    assert body["items"][0]["baseline_set"] is False


def test_watchlist_alerts_partial_failure(client, monkeypatch, mem_db):
    _use_owner(monkeypatch, "keyE")
    client.post("/v1/watchlist", json={"query": "ok query"})
    client.post("/v1/watchlist", json={"query": "broken query"})
    pipe = FakePipeline(results=[_result("https://a.com/1")], fail_for={"broken"})
    monkeypatch.setattr(deps, "get_search_pipeline", lambda: pipe)
    body = client.get("/v1/watchlist/alerts").json()
    assert len(body["items"]) == 2
    by_query = {i["query"]: i for i in body["items"]}
    assert by_query["ok query"]["error"] is None
    assert (
        by_query["broken query"]["error"] == "search_failed"
    )  # stable code, no internal detail leak


# --- podcast --------------------------------------------------------------------


def test_briefing_script_builder_pure():
    sections = [
        briefing.BriefingSection(
            label="Kinh tế vĩ mô",
            items=[{"title": "GDP **tăng** 7% [1] https://x.com", "url": "u", "published_at": ""}],
        ),
        briefing.BriefingSection(label="Trống", items=[]),
    ]
    day = datetime(2026, 11, 12, tzinfo=UTC).date()
    script = briefing.build_briefing_script(sections, max_chars=4000, today=day)
    assert script.startswith("Bản tin MetaSearch ngày 12 tháng 11 năm 2026.")
    assert "Kinh tế vĩ mô." in script
    assert "GDP tăng 7%" in script  # markdown stripped
    assert "https://" not in script
    assert "Trống." not in script
    # cap truncates at a sentence boundary, never mid-word
    long_items = [{"title": f"câu số {i}." * 20, "url": "u", "published_at": ""} for i in range(50)]
    long_sections = [briefing.BriefingSection(label="L", items=long_items)]
    capped = briefing.build_briefing_script(long_sections, max_chars=200, today=day)
    assert len(capped) <= 200
    assert capped.endswith(".")
    assert briefing.build_briefing_script([], max_chars=100).startswith("Bản tin")


def test_briefing_storage_helpers(tmp_path):
    day = datetime(2026, 11, 12, tzinfo=UTC).date()
    p = briefing.write_briefing(tmp_path, day, b"audio-bytes")
    assert p.name == "briefing-2026-11-12.mp3"
    assert briefing.latest_briefing(tmp_path) == p
    # evil names are ignored
    (tmp_path / "briefing-2020-01-01.mp3").write_bytes(b"old")
    (tmp_path / "random.txt").write_text("x")
    assert briefing.latest_briefing(tmp_path).name == "briefing-2026-11-12.mp3"
    # retention removes only old valid briefing files
    removed = briefing.cleanup_retention(tmp_path, retention_days=7, today=day)
    assert removed == 1
    assert briefing.latest_briefing(tmp_path) == p
    assert (tmp_path / "random.txt").exists()


def test_podcast_disabled_503(client, monkeypatch, tmp_path):
    from app.api.v1 import podcast

    monkeypatch.setattr(deps, "get_tts_client", lambda: FakeTtsClient(enabled=False))
    monkeypatch.setattr(get_settings(), "podcast_data_dir", str(tmp_path))
    podcast.reset_podcast_state()
    for path in ("/v1/podcast/briefing",):
        resp = client.post(path)
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "tts_disabled"
    assert client.get("/v1/podcast/briefing/status").status_code == 503
    assert client.get("/v1/podcast/briefing/latest").status_code == 503
    assert client.get("/v1/podcast/briefing/feed.xml").status_code == 503


def test_podcast_full_flow(client, monkeypatch, tmp_path):
    from app.api.v1 import podcast

    monkeypatch.setattr(deps, "get_tts_client", lambda: FakeTtsClient(enabled=True))
    pipe = FakePipeline(results=[_result("https://a.com/1", "Tin hot")])
    monkeypatch.setattr(deps, "get_search_pipeline", lambda: pipe)
    monkeypatch.setattr(get_settings(), "podcast_data_dir", str(tmp_path))
    podcast.reset_podcast_state()

    resp = client.post("/v1/podcast/briefing")
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] in ("pending", "running", "ready")

    # Drive the portal loop until the background job finishes.
    import time as _time

    deadline = _time.time() + 15
    status = None
    while _time.time() < deadline:
        r = client.get("/v1/podcast/briefing/status")
        assert r.status_code == 200
        status = r.json()
        if status["status"] in ("ready", "error"):
            break
        _time.sleep(0.2)
    assert status and status["status"] == "ready", status
    assert status["chars"] > 0 and status["audio_bytes"] == len(b"ID3-fake-mp3")

    # idempotent: second POST returns the ready briefing
    assert client.post("/v1/podcast/briefing").json()["status"] == "ready"

    latest = client.get("/v1/podcast/briefing/latest")
    assert latest.status_code == 200
    assert latest.headers["content-type"].startswith("audio/mpeg")
    assert latest.content == b"ID3-fake-mp3"

    feed = client.get("/v1/podcast/briefing/feed.xml")
    assert feed.status_code == 200
    assert "rss" in feed.text and "audio/mpeg" in feed.text
    assert "/v1/podcast/briefing/latest" in feed.text


def test_podcast_latest_404_when_empty(client, monkeypatch, tmp_path):
    from app.api.v1 import podcast

    monkeypatch.setattr(deps, "get_tts_client", lambda: FakeTtsClient(enabled=True))
    monkeypatch.setattr(get_settings(), "podcast_data_dir", str(tmp_path))
    podcast.reset_podcast_state()
    assert client.get("/v1/podcast/briefing/latest").status_code == 404
    assert client.get("/v1/podcast/briefing/feed.xml").status_code == 404


def test_fa_compare_csv_export(client, monkeypatch):
    """Phase 4 P0-1: format=csv returns a UTF-8-BOM CSV table with per-symbol
    provenance; format guard rejects unknown values."""
    from datetime import UTC, datetime

    from app.api import deps
    from app.retrieval.fa_sources.aggregator import AggregatedFinancials, FieldValue

    def _agg_with_pe(symbol: str, pe: float) -> AggregatedFinancials:
        agg = AggregatedFinancials(symbol=symbol)
        agg.fields["pe"] = FieldValue(
            value=pe, source="tcbs", as_of=datetime(2026, 9, 30, tzinfo=UTC), validated=True
        )
        agg.missing_fields = [f for f in agg.missing_fields if f != "pe"]
        agg.sources_used = ["tcbs"]
        agg.source_urls = [f"https://example.com/{symbol}"]
        agg.as_of = datetime(2026, 9, 30, tzinfo=UTC)
        return agg

    class _FakeAgg:
        async def fetch_financials(self, symbol, *, timeout_s=3.5):
            if symbol == "VCB":
                return _agg_with_pe(symbol, 9.5)
            return AggregatedFinancials(symbol=symbol)

    monkeypatch.setattr(deps, "get_fa_aggregator", lambda: _FakeAgg())

    resp = client.post("/v1/fa/compare", json={"symbols": ["VCB", "FPT"], "format": "csv"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    body = resp.content.decode("utf-8-sig")
    assert "metric,VCB,FPT" in body
    assert "pe,9.5," in body  # FPT honestly empty (no grounded data)
    assert "sources,tcbs," in body

    resp_bad = client.post("/v1/fa/compare", json={"symbols": ["VCB"], "format": "xlsx"})
    assert resp_bad.status_code == 400
