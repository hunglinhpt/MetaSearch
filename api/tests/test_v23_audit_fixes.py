"""Audit-round fixes for docs/18 v2.3.0 (expert loop 1 findings).

Covers the degradation branches the first test file did not: watchlist
in-memory fallback + incomplete-outcome baseline guard, podcast job error
genericity + double-POST single-flight + RSS validity, market/ohlcv refusing
timestamp-less series, and the strict VNDIRECT dchart parser.
"""

from __future__ import annotations

import os
import re
import time as _time
import uuid
from collections import OrderedDict
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api import deps
from app.api.v1 import market, podcast, watchlist
from app.config import get_settings
from app.retrieval.adapters.viet_stock import _parse_dchart_series
from app.retrieval.fa_sources.base import SourceOHLCV
from app.retrieval.models import ScoredResult
from app.tts import briefing


def _result(url: str, title: str = "t") -> ScoredResult:

    return ScoredResult(
        url=url,
        canonical_url=url,
        title=title,
        snippet="s",
        domain="example.com",
        published_at=datetime.now(UTC),
    )


class FakePipeline:
    def __init__(self, results=None):
        self._results = results if results is not None else []

    async def run(self, params, on_event=None):
        from app.orchestration.pipeline import SearchOutcome

        return SearchOutcome(
            query_id=uuid.uuid4(), results=list(self._results), statuses=[], latency_ms=1
        )


class FakeTtsClient:
    def __init__(self, enabled=True, audio=b"ID3-fake-mp3"):
        self._enabled = enabled
        self._audio = audio

    @property
    def enabled(self):
        return self._enabled

    async def synthesize(self, text, *, voice=None, model=None, response_format="mp3"):
        return self._audio, "audio/mpeg"


class FakeAggregator:
    def __init__(self, ohlcv=None):
        self._ohlcv = ohlcv

    async def fetch_ohlcv(self, symbol, *, timeout_s=3.5):
        return self._ohlcv


def _use_owner(monkeypatch, owner: str):
    monkeypatch.setattr(watchlist, "_owner", lambda: owner)


@pytest.fixture
def mem_db(monkeypatch):
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


def test_watchlist_memory_fallback_when_db_down(client, monkeypatch):
    """DB read AND write fail -> sticky in-memory mode, response says so."""
    _use_owner(monkeypatch, "keyMem")

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(deps, "get_session_factory", boom)
    monkeypatch.setattr("app.db.session.get_engine", boom)
    monkeypatch.setattr(watchlist, "_table_ready", False)
    monkeypatch.setattr(watchlist, "_mem_owners", set())
    monkeypatch.setattr(watchlist, "_mem_store", OrderedDict())

    resp = client.post("/v1/watchlist", json={"query": "VCB"})
    assert resp.status_code == 201, resp.text
    body = client.get("/v1/watchlist").json()
    assert body["storage"] == "memory"
    assert [i["query"] for i in body["items"]] == ["VCB"]


def test_watchlist_alerts_incomplete_outcome_keeps_baseline(client, monkeypatch, mem_db):
    """A degraded engine (complete=False) must NOT advance the baseline."""
    from app.orchestration.pipeline import SearchOutcome

    _use_owner(monkeypatch, "keyInc")
    client.post("/v1/watchlist", json={"query": "FPT"})

    class SeqPipeline:
        def __init__(self):
            self.calls = 0

        async def run(self, params, on_event=None):
            self.calls += 1
            if self.calls == 1:
                return SearchOutcome(
                    query_id=uuid.uuid4(),
                    results=[_result("https://a.com/1")],
                    statuses=[],
                    latency_ms=1,
                )
            return SearchOutcome(
                query_id=uuid.uuid4(), results=[], statuses=[], latency_ms=1, complete=False
            )

    pipe = SeqPipeline()
    monkeypatch.setattr(deps, "get_search_pipeline", lambda: pipe)
    first = client.get("/v1/watchlist/alerts").json()
    assert first["items"][0]["baseline_set"] is True
    assert first["storage"] == "db"
    second = client.get("/v1/watchlist/alerts").json()
    assert second["items"][0]["error"] == "search_incomplete"
    assert second["total_new"] == 0

    class GoodPipeline:
        async def run(self, params, on_event=None):
            return SearchOutcome(
                query_id=uuid.uuid4(),
                results=[_result("https://a.com/1"), _result("https://a.com/9", "moi")],
                statuses=[],
                latency_ms=1,
            )

    monkeypatch.setattr(deps, "get_search_pipeline", lambda: GoodPipeline())
    third = client.get("/v1/watchlist/alerts").json()
    assert third["total_new"] == 1
    assert third["items"][0]["new_items"][0]["url"] == "https://a.com/9"


def test_podcast_job_error_is_generic(client, monkeypatch, tmp_path):
    class FailTts(FakeTtsClient):
        async def synthesize(self, text, **kw):
            raise RuntimeError("voicestudio internal http://10.0.0.5:3900 detail")

    monkeypatch.setattr(deps, "get_tts_client", lambda: FailTts(enabled=True))
    monkeypatch.setattr(
        deps,
        "get_search_pipeline",
        lambda: FakePipeline(results=[_result("https://a.com/1", "Tin")]),
    )
    monkeypatch.setattr(get_settings(), "podcast_data_dir", str(tmp_path))
    podcast.reset_podcast_state()

    assert client.post("/v1/podcast/briefing").status_code == 202
    deadline = _time.time() + 15
    status = None
    while _time.time() < deadline:
        status = client.get("/v1/podcast/briefing/status").json()
        if status["status"] in ("ready", "error"):
            break
        _time.sleep(0.2)
    assert status["status"] == "error"
    # generic code to the client — no internal host/URL leak
    assert status["error"] == "briefing_failed"
    assert "10.0.0.5" not in str(status)


def test_podcast_double_post_single_job_and_valid_rss(client, monkeypatch, tmp_path):
    import asyncio
    import xml.etree.ElementTree as ET

    class SlowTts(FakeTtsClient):
        async def synthesize(self, text, **kw):
            await asyncio.sleep(1.0)  # keep the job "running" across both POSTs
            return await super().synthesize(text, **kw)

    monkeypatch.setattr(deps, "get_tts_client", lambda: SlowTts(enabled=True))
    monkeypatch.setattr(
        deps,
        "get_search_pipeline",
        lambda: FakePipeline(results=[_result("https://a.com/1", "Tin")]),
    )
    monkeypatch.setattr(get_settings(), "podcast_data_dir", str(tmp_path))
    podcast.reset_podcast_state()

    r1 = client.post("/v1/podcast/briefing")
    r2 = client.post("/v1/podcast/briefing")
    assert r1.status_code == 202 and r2.status_code == 202
    assert r2.json()["status"] == "running"  # single-flight: no second job
    assert len(podcast._tasks) <= 1

    deadline = _time.time() + 15
    s = None
    while _time.time() < deadline:
        s = client.get("/v1/podcast/briefing/status").json()
        if s["status"] in ("ready", "error"):
            break
        _time.sleep(0.2)
    assert s["status"] == "ready"

    feed = client.get("/v1/podcast/briefing/feed.xml")
    assert feed.status_code == 200
    assert feed.headers["cache-control"].startswith("private")
    root = ET.fromstring(feed.text)
    assert root.tag == "rss" and root.get("version") == "2.0"
    enclosure = root.find("./channel/item/enclosure")
    assert enclosure is not None
    assert enclosure.get("type") == "audio/mpeg"
    assert enclosure.get("url", "").endswith("/v1/podcast/briefing/latest")
    assert int(enclosure.get("length", "0")) == len(b"ID3-fake-mp3")
    latest = client.get("/v1/podcast/briefing/latest")
    assert latest.headers["cache-control"] == "private, no-store"


def test_market_ohlcv_rejects_series_without_times(client, monkeypatch):
    """Closes but NO per-candle times -> 503; never synthesized timestamps."""

    async def none_series(symbol, *, days, timeout_s, **kw):
        return None

    src = SourceOHLCV(
        source_id="ssi",
        opens=[1.0] * 6,
        highs=[2.0] * 6,
        lows=[0.5] * 6,
        closes=[1.5] * 6,
        volumes=[10.0] * 6,
        times=[],
        last_ts=1_760_000_000,
        source_url="https://ssi.example/x",
    )
    monkeypatch.setattr(market, "fetch_vndirect_ohlcv_series", none_series)
    monkeypatch.setattr(deps, "get_fa_aggregator", lambda: FakeAggregator(ohlcv=src))
    resp = client.post("/v1/market/ohlcv", json={"symbol": "VCB"})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "no_results"


def test_parse_dchart_series_strict_arrays():
    """VNDIRECT parser refuses schema drift instead of fabricating OHLC/volume."""
    base = {"s": "ok", "t": [1, 2, 3, 4, 5, 6], "c": [10, 11, 12, 13, 14, 15]}
    assert _parse_dchart_series(dict(base), "VCB") is None  # missing o/h/l/v
    drift = dict(base, o=[1, 2], h=base["c"], l=base["c"], v=base["c"])
    assert _parse_dchart_series(drift, "VCB") is None  # short o array
    bad_t = dict(base, o=base["c"], h=base["c"], l=base["c"], v=base["c"], t=[6, 5, 4, 3, 2, 1])
    assert _parse_dchart_series(bad_t, "VCB") is None  # non-ascending times
    good = dict(base, o=base["c"], h=base["c"], l=base["c"], v=[1.0] * 6)
    out = _parse_dchart_series(good, "VCB")
    assert out is not None and out["closes"][-1] == 15 * 1000.0
    idx = _parse_dchart_series(dict(good, c=[5000, 5001, 5002, 5003, 5004, 5005]), "VNINDEX")
    assert idx is not None and idx["closes"][-1] == 5005.0  # index not scaled


def test_briefing_retention_keeps_exactly_n_days_and_cleans_tmp(tmp_path):
    today = date(2026, 11, 12)
    for i in range(10):
        day = today - timedelta(days=i)
        briefing.write_briefing(tmp_path, day, b"x")
    # Orphan tmp from a crashed write: aged past the 1h safety window so it is
    # reaped; a FRESH tmp (a write in flight) must survive — see next test.
    orphan = tmp_path / "briefing-2026-11-12.mp3.tmp"
    orphan.write_bytes(b"partial")
    old = _time.time() - 7200
    os.utime(orphan, (old, old))
    removed = briefing.cleanup_retention(tmp_path, retention_days=7, today=today)
    assert removed == 3 + 1  # days 7,8,9 + the orphan tmp
    remaining = sorted(p.name for p in tmp_path.iterdir())
    expected = sorted(f"briefing-{(today - timedelta(days=i)).isoformat()}.mp3" for i in range(7))
    assert remaining == expected


def test_briefing_retention_keeps_fresh_tmp_of_live_write(tmp_path):
    """Loop-3: retention must NOT unlink a .tmp that a concurrent write_briefing
    is actively filling (write_bytes -> replace); only aged tmps are reaped."""
    today = date(2026, 11, 12)
    briefing.write_briefing(tmp_path, today, b"x")
    fresh = tmp_path / "briefing-2026-11-11.mp3.tmp"
    fresh.write_bytes(b"partial")  # mtime = now -> protected
    removed = briefing.cleanup_retention(tmp_path, retention_days=7, today=today)
    assert removed == 0
    assert fresh.is_file()


def test_parse_dchart_series_rejects_nan_in_any_array():
    """Loop-2 P2-1: bare NaN (json.loads accepts it) in ANY of o/h/l/v must be
    refused — otherwise the API would 500 via allow_nan=False."""
    base = {"s": "ok", "t": [1, 2, 3, 4, 5, 6], "c": [10, 11, 12, 13, 14, 15]}
    full = dict(base, o=base["c"], h=base["c"], l=base["c"], v=[1.0] * 6)
    for field in ("o", "h", "l", "c", "v"):
        bad = dict(full)
        arr = list(full[field])
        arr[2] = float("nan")
        bad[field] = arr
        assert _parse_dchart_series(bad, "VCB") is None, field
        arr[2] = float("inf")
        bad[field] = arr
        assert _parse_dchart_series(bad, "VCB") is None, field
        arr[2] = -5.0
        bad[field] = arr
        assert _parse_dchart_series(bad, "VCB") is None, field


def test_watchlist_memory_mode_recovers_to_db(client, monkeypatch, mem_db):
    """Loop-2 P2-3: after a DB outage the owner is memory-sticky, but a
    throttled re-probe must migrate items back to the DB once it recovers."""
    import time as _t

    _use_owner(monkeypatch, "keyRec")

    # Phase 1: DB down -> memory mode.
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(deps, "get_session_factory", boom)
    monkeypatch.setattr("app.db.session.get_engine", boom)
    monkeypatch.setattr(watchlist, "_table_ready", False)
    monkeypatch.setattr(watchlist, "_mem_owners", set())
    monkeypatch.setattr(watchlist, "_mem_probe_at", {})
    monkeypatch.setattr(watchlist, "_mem_store", OrderedDict())
    assert client.post("/v1/watchlist", json={"query": "VCB"}).status_code == 201
    assert client.get("/v1/watchlist").json()["storage"] == "memory"

    # Phase 2: DB back — re-point the factory at a working sqlite engine and
    # force the probe window open.
    engine2 = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    factory2 = async_sessionmaker(engine2, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(deps, "get_session_factory", lambda: factory2)
    monkeypatch.setattr("app.db.session.get_engine", lambda: engine2)
    monkeypatch.setattr(watchlist, "_table_ready", False)
    watchlist._mem_probe_at["keyRec"] = _t.monotonic() - 9999  # probe due now
    body = client.get("/v1/watchlist").json()
    assert body["storage"] == "db"
    assert [i["query"] for i in body["items"]] == ["VCB"]  # migrated, not lost


def test_watchlist_delete_during_outage_is_not_resurrected(client, monkeypatch, mem_db):
    """Loop-3: the memory list is the authoritative id set during an outage —
    an item deleted while the DB was down must NOT come back when the DB
    recovers and the owner migrates back."""
    import time as _t

    _use_owner(monkeypatch, "keyDel")

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(deps, "get_session_factory", boom)
    monkeypatch.setattr("app.db.session.get_engine", boom)
    monkeypatch.setattr(watchlist, "_table_ready", False)
    monkeypatch.setattr(watchlist, "_mem_owners", set())
    monkeypatch.setattr(watchlist, "_mem_probe_at", {})
    monkeypatch.setattr(watchlist, "_mem_store", OrderedDict())
    r1 = client.post("/v1/watchlist", json={"query": "VCB"})
    r2 = client.post("/v1/watchlist", json={"query": "HPG"})
    assert r1.status_code == 201 and r2.status_code == 201
    # Delete HPG while still in memory mode.
    assert client.delete(f"/v1/watchlist/{r2.json()['id']}").status_code == 200
    assert [i["query"] for i in client.get("/v1/watchlist").json()["items"]] == ["VCB"]

    # DB recovers: migration must persist ONLY VCB (HPG stays deleted).
    engine2 = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    factory2 = async_sessionmaker(engine2, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(deps, "get_session_factory", lambda: factory2)
    monkeypatch.setattr("app.db.session.get_engine", lambda: engine2)
    monkeypatch.setattr(watchlist, "_table_ready", False)
    watchlist._mem_probe_at["keyDel"] = _t.monotonic() - 9999
    body = client.get("/v1/watchlist").json()
    assert body["storage"] == "db"
    assert [i["query"] for i in body["items"]] == ["VCB"]


def test_parse_dchart_series_rejects_non_numeric_last_close():
    """Loop-3: the /1000 multiplier probe reads the raw last close — a
    non-numeric tail must refuse the series (None), never raise."""
    base = {"s": "ok", "t": [1, 2, 3, 4, 5, 6], "c": [10, 11, 12, 13, 14, "abc"]}
    full = dict(base, o=base["c"], h=base["c"], l=base["c"], v=[1.0] * 6)
    assert _parse_dchart_series(full, "VCB") is None
    full2 = dict(base, c=[10, 11, 12, 13, 14, float("nan")])
    full2 = dict(full2, o=full2["c"], h=full2["c"], l=full2["c"], v=[1.0] * 6)
    assert _parse_dchart_series(full2, "VCB") is None


def test_market_ohlcv_wall_clock_deadline_is_504(client, monkeypatch):
    """Loop-3: when BOTH legs blow the wall clock the answer is 504
    deadline_exceeded (same contract as fa/compare), not a misleading 503."""
    import asyncio

    from app.api.v1 import market

    async def hang(*a, **kw):
        await asyncio.sleep(30)

    class _HangAgg:
        async def fetch_ohlcv(self, symbol, *, timeout_s=3.5):
            await asyncio.sleep(30)

    monkeypatch.setattr(market, "fetch_vndirect_ohlcv_series", hang)
    monkeypatch.setattr(deps, "get_fa_aggregator", lambda: _HangAgg())
    monkeypatch.setattr(get_settings(), "market_ohlcv_timeout_s", 0.2)
    resp = client.post("/v1/market/ohlcv", json={"symbol": "VCB", "days": 30})
    assert resp.status_code == 504
    assert resp.json()["error"]["code"] == "deadline_exceeded"


# --- Loop-3 fixes ------------------------------------------------------------


class _RaisingAggregator:
    async def fetch_ohlcv(self, symbol, *, timeout_s=3.5):
        raise RuntimeError("aggregator exploded with internal host 10.0.0.5")


def test_market_ohlcv_fallback_exception_is_503_not_500(client, monkeypatch):
    """Loop-3: a raising fallback aggregator must degrade to 503 no_results,
    never a 500 leaking the exception."""
    from app.api.v1 import market

    async def none_series(*a, **kw):
        return None

    monkeypatch.setattr(market, "fetch_vndirect_ohlcv_series", none_series)
    monkeypatch.setattr(deps, "get_fa_aggregator", lambda: _RaisingAggregator())
    resp = client.post("/v1/market/ohlcv", json={"symbol": "VCB", "days": 30})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "no_results"
    assert "10.0.0.5" not in resp.text


@pytest.mark.asyncio
async def test_owner_lock_eviction_never_drops_a_held_lock():
    """Loop-3 P2: when the lock map is full, eviction must skip locks that are
    held/contended — deleting one would alias two locks to the same owner."""
    from app.api.v1 import watchlist as wl

    saved = (dict(wl._owner_locks), wl._OWNER_LOCKS_MAX)
    try:
        wl._owner_locks.clear()
        wl._OWNER_LOCKS_MAX = 4
        held_owner = "held"
        held = await wl._owner_lock(held_owner)
        await held.acquire()  # simulate an in-flight request holding it
        try:
            for i in range(10):
                await wl._owner_lock(f"other{i}")
            # The held lock must still be the SAME object under its owner.
            assert wl._owner_locks.get(held_owner) is held
            again = await wl._owner_lock(held_owner)
            assert again is held
            assert len(wl._owner_locks) <= wl._OWNER_LOCKS_MAX + 1
        finally:
            held.release()
    finally:
        wl._owner_locks.clear()
        wl._owner_locks.update(saved[0])
        wl._OWNER_LOCKS_MAX = saved[1]


@pytest.mark.asyncio
async def test_podcast_cancelled_job_does_not_stay_running(monkeypatch, tmp_path):
    """Loop-3: a cancelled synthesis task must mark the job 'cancelled', not
    leave it 'running' forever (which would block every later POST)."""
    import asyncio

    from app.api.v1 import podcast

    class _HangingPipeline:
        async def run(self, params, on_event=None):
            await asyncio.sleep(3600)

    class _SlowTts(FakeTtsClient):
        async def synthesize(self, text, **kw):
            await asyncio.sleep(3600)

    monkeypatch.setattr(deps, "get_search_pipeline", lambda: _HangingPipeline())
    monkeypatch.setattr(deps, "get_tts_client", lambda: _SlowTts())
    monkeypatch.setattr(get_settings(), "podcast_data_dir", str(tmp_path))
    podcast.reset_podcast_state()
    day = podcast._today()
    key = day.isoformat()
    podcast._jobs[key] = {
        "status": "running",
        "created_at": None,
        "error": None,
        "chars": 0,
        "audio_bytes": 0,
    }
    task = asyncio.create_task(podcast._run_briefing(day))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert podcast._jobs[key]["status"] == "cancelled"


def test_podcast_feed_rejects_forged_scheme_in_public_base_url(client, monkeypatch, tmp_path):
    """Loop-3: a non-http(s) public_base_url must never reach the RSS enclosure."""
    from app.api.v1 import podcast

    monkeypatch.setattr(deps, "get_tts_client", lambda: FakeTtsClient(enabled=True))
    monkeypatch.setattr(get_settings(), "podcast_data_dir", str(tmp_path))
    monkeypatch.setattr(get_settings(), "public_base_url", "javascript://evil.example")
    podcast.reset_podcast_state()
    day = date(2026, 11, 12)
    briefing.write_briefing(tmp_path, day, b"ID3")
    resp = client.get("/v1/podcast/briefing/feed.xml")
    assert resp.status_code == 200
    assert "javascript:" not in resp.text
    # Falls back to the request origin (http here) or a relative path — never
    # the forged scheme.
    m = re.search(r'enclosure url="([^"]+)"', resp.text)
    assert m and (m.group(1).startswith(("http://", "https://")) or m.group(1).startswith("/"))
