"""Personal podcast (P2-5, docs/18 D): owner-scoped briefing flow, disabled
gating, honest error status, and empty-file 404 — all offline via fakes."""

from __future__ import annotations

import time

import pytest

from app.api import deps
from app.config import get_settings
from tests.test_market_watchlist_podcast import FakePipeline, FakeTtsClient, _result


@pytest.fixture
def personal_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "podcast_personal_enabled", True)
    monkeypatch.setattr(get_settings(), "podcast_data_dir", str(tmp_path))
    from app.api.v1 import podcast

    podcast.reset_podcast_state()
    return podcast


def test_personal_disabled_400(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "podcast_personal_enabled", False)
    monkeypatch.setattr(deps, "get_tts_client", lambda: FakeTtsClient(enabled=True))
    resp = client.post("/v1/podcast/personal")
    assert resp.status_code == 400
    assert "PODCAST_PERSONAL_ENABLED" in resp.json()["error"]["message"]
    assert client.get("/v1/podcast/personal/status").status_code == 400
    assert client.get("/v1/podcast/personal/latest").status_code == 400


def test_personal_tts_disabled_503(client, personal_enabled, monkeypatch):
    monkeypatch.setattr(deps, "get_tts_client", lambda: FakeTtsClient(enabled=False))
    resp = client.post("/v1/podcast/personal")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "tts_disabled"
    assert client.get("/v1/podcast/personal/status").status_code == 503
    assert client.get("/v1/podcast/personal/latest").status_code == 503


def test_personal_status_none_and_latest_404(client, personal_enabled, monkeypatch):
    monkeypatch.setattr(deps, "get_tts_client", lambda: FakeTtsClient(enabled=True))
    body = client.get("/v1/podcast/personal/status").json()
    assert body["status"] == "none"
    assert client.get("/v1/podcast/personal/latest").status_code == 404


def test_personal_full_flow_ready_with_watchlist_fallback(client, personal_enabled, monkeypatch):
    """Dead DB -> watchlist read fails -> global presets fallback (never fabricated);
    pipeline + fake TTS produce the owner-scoped personal-*.mp3."""
    monkeypatch.setattr(deps, "get_tts_client", lambda: FakeTtsClient(enabled=True))
    pipe = FakePipeline(results=[_result("https://a.com/1", "FPT công bố kết quả kinh doanh")])
    monkeypatch.setattr(deps, "get_search_pipeline", lambda: pipe)

    resp = client.post("/v1/podcast/personal")
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "running"

    status = None
    deadline = time.time() + 15
    while time.time() < deadline:
        r = client.get("/v1/podcast/personal/status")
        assert r.status_code == 200
        status = r.json()
        if status["status"] in ("ready", "error"):
            break
        time.sleep(0.2)
    assert status and status["status"] == "ready", status
    assert status["chars"] > 0 and status["audio_bytes"] == len(b"ID3-fake-mp3")

    # idempotent per owner per day
    assert client.post("/v1/podcast/personal").json()["status"] == "ready"

    latest = client.get("/v1/podcast/personal/latest")
    assert latest.status_code == 200
    assert latest.headers["content-type"].startswith("audio/mpeg")
    assert latest.content == b"ID3-fake-mp3"


def test_personal_briefing_error_status_when_pipeline_fails(client, personal_enabled, monkeypatch):
    monkeypatch.setattr(deps, "get_tts_client", lambda: FakeTtsClient(enabled=True))
    pipe = FakePipeline(results=[])  # no results at all -> NO_RESULTS error path
    monkeypatch.setattr(deps, "get_search_pipeline", lambda: pipe)

    resp = client.post("/v1/podcast/personal")
    assert resp.status_code == 202

    status = None
    deadline = time.time() + 15
    while time.time() < deadline:
        r = client.get("/v1/podcast/personal/status")
        status = r.json()
        if status["status"] in ("ready", "error"):
            break
        time.sleep(0.2)
    assert status and status["status"] == "error", status
    assert status["error"] == "briefing_failed"
    # failed job does not create an audio file
    assert client.get("/v1/podcast/personal/latest").status_code == 404
