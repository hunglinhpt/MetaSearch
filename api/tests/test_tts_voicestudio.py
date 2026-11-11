"""Tests for the optional VoiceStudio server-side TTS proxy (docs/17)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.api import deps
from app.config import Settings
from app.core.errors import ErrorCode, MetaSearchError
from app.main import app
from app.tts.voicestudio import VoiceStudioClient


@pytest.fixture(autouse=True)
def _clean_tts_state():
    from app.api.v1 import tts as tts_route

    tts_route.reset_tts_state()
    yield
    tts_route.reset_tts_state()


def _settings(**kw) -> Settings:
    kw.setdefault("voicestudio_url", "http://vs.test:3900")
    kw.setdefault("voicestudio_api_key", "secret-key")
    return Settings(**kw)


def _vs(handler, **kw) -> VoiceStudioClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return VoiceStudioClient(_settings(**kw), client=http)


def _api_client(monkeypatch, vs: VoiceStudioClient, **settings_kw) -> httpx.AsyncClient:
    monkeypatch.setattr(deps, "get_tts_client", lambda: vs)
    monkeypatch.setattr("app.api.v1.tts.get_settings", lambda: _settings(**settings_kw))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------- client unit


def test_disabled_when_no_url():
    vs = VoiceStudioClient(Settings(voicestudio_url=""), client=httpx.AsyncClient())
    assert vs.enabled is False
    assert vs.capabilities() == {"voicestudio": {"enabled": False, "model": None, "voice": None}}


@pytest.mark.parametrize(
    "bad",
    ["javascript:alert(1)", "ftp://x", "http://user:pass@host", "http://host/path?q=1", "notaurl"],
)
def test_invalid_or_embellished_url_treated_as_disabled(bad):
    vs = VoiceStudioClient(Settings(voicestudio_url=bad), client=httpx.AsyncClient())
    assert vs.enabled is False


async def test_synthesize_sends_openai_compatible_payload():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"AUDIO", headers={"content-type": "audio/mpeg"})

    vs = _vs(handler, voicestudio_model="omnivoice", voicestudio_voice="demo0001")
    audio, media = await vs.synthesize("Xin chào", voice=None, model=None, response_format="mp3")
    assert audio == b"AUDIO"
    assert media == "audio/mpeg"
    req = captured[0]
    assert req.url == "http://vs.test:3900/v1/audio/speech"
    body = json.loads(req.content)
    assert body == {
        "model": "omnivoice",
        "input": "Xin chào",
        "response_format": "mp3",
        "voice": "demo0001",
    }
    assert req.headers["Authorization"] == "Bearer secret-key"


async def test_synthesize_overrides_and_no_key():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"WAV", headers={"content-type": "audio/wav"})

    vs = _vs(handler, voicestudio_api_key="")
    await vs.synthesize("hello", voice="abc", model="kokoro", response_format="wav")
    body = json.loads(captured[0].content)
    assert body["voice"] == "abc" and body["model"] == "kokoro"
    assert "authorization" not in {k.lower() for k in captured[0].headers}


async def test_synthesize_upstream_error_status():
    vs = _vs(lambda req: httpx.Response(500, text="boom"))
    with pytest.raises(MetaSearchError) as exc:
        await vs.synthesize("x")
    assert exc.value.code == ErrorCode.TTS_UPSTREAM_ERROR
    assert exc.value.status_code == 502


async def test_synthesize_timeout_maps_to_504():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    vs = _vs(handler)
    with pytest.raises(MetaSearchError) as exc:
        await vs.synthesize("x")
    assert exc.value.code == ErrorCode.UPSTREAM_TIMEOUT
    assert exc.value.status_code == 504


async def test_synthesize_audio_size_cap():
    vs = _vs(
        lambda req: httpx.Response(
            200, content=b"TOOLONGAUDIO", headers={"content-type": "audio/mpeg"}
        ),
        tts_max_audio_bytes=4,
    )
    with pytest.raises(MetaSearchError) as exc:
        await vs.synthesize("x")
    assert exc.value.code == ErrorCode.TTS_UPSTREAM_ERROR
    assert exc.value.status_code == 502


async def test_synthesize_when_disabled_raises_503():
    vs = VoiceStudioClient(Settings(voicestudio_url=""), client=httpx.AsyncClient())
    with pytest.raises(MetaSearchError) as exc:
        await vs.synthesize("x")
    assert exc.value.code == ErrorCode.TTS_DISABLED
    assert exc.value.status_code == 503


async def test_synthesize_wall_clock_deadline():
    # A trickling upstream never trips the per-read timeout; the asyncio.timeout
    # wall-clock budget must still fire.
    import asyncio

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(200, content=b"AUDIO")

    vs = _vs(handler, voicestudio_timeout_s=0.02)
    with pytest.raises(MetaSearchError) as exc:
        await vs.synthesize("x")
    assert exc.value.code == ErrorCode.UPSTREAM_TIMEOUT
    assert exc.value.status_code == 504


async def test_synthesize_media_type_whitelist():
    # Upstream content-type outside the audio whitelist must not be passed through.
    vs = _vs(
        lambda req: httpx.Response(200, content=b"AUDIO", headers={"content-type": "text/html"})
    )
    _audio, media = await vs.synthesize("x", response_format="mp3")
    assert media == "audio/mpeg"


# ------------------------------------------------------------------- routes


async def test_route_capabilities_disabled(monkeypatch):
    vs = VoiceStudioClient(Settings(voicestudio_url=""), client=httpx.AsyncClient())
    c = _api_client(monkeypatch, vs, voicestudio_url="")
    async with c:
        resp = await c.get("/v1/tts/capabilities")
    assert resp.status_code == 200
    assert resp.json() == {"voicestudio": {"enabled": False, "model": None, "voice": None}}


async def test_route_synthesize_503_when_disabled(monkeypatch):
    vs = VoiceStudioClient(Settings(voicestudio_url=""), client=httpx.AsyncClient())
    c = _api_client(monkeypatch, vs, voicestudio_url="")
    async with c:
        resp = await c.post("/v1/tts/synthesize", json={"text": "tin mới"})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "tts_disabled"


async def test_route_synthesize_400_when_text_too_long(monkeypatch):
    vs = _vs(lambda req: httpx.Response(200, content=b"A"))
    c = _api_client(monkeypatch, vs, tts_max_chars=10)
    async with c:
        resp = await c.post("/v1/tts/synthesize", json={"text": "x" * 11})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"


async def test_route_synthesize_200_etag_and_304(monkeypatch):
    vs = _vs(
        lambda req: httpx.Response(200, content=b"AUDIO", headers={"content-type": "audio/mpeg"})
    )
    c = _api_client(monkeypatch, vs)
    async with c:
        resp = await c.post("/v1/tts/synthesize", json={"text": "tin mới"})
        assert resp.status_code == 200
        assert resp.content == b"AUDIO"
        assert resp.headers["content-type"].startswith("audio/mpeg")
        etag = resp.headers["etag"]
        assert etag and resp.headers["cache-control"]
        resp2 = await c.post(
            "/v1/tts/synthesize", json={"text": "tin mới"}, headers={"If-None-Match": etag}
        )
        assert resp2.status_code == 304
        # Different text -> different ETag -> 200 again
        resp3 = await c.post("/v1/tts/synthesize", json={"text": "tin khác"})
        assert resp3.status_code == 200
        assert resp3.headers["etag"] != etag


async def test_route_synthesize_502_on_upstream_failure(monkeypatch):
    vs = _vs(lambda req: httpx.Response(503, text="model loading"))
    c = _api_client(monkeypatch, vs)
    async with c:
        resp = await c.post("/v1/tts/synthesize", json={"text": "tin"})
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "tts_upstream_error"


async def test_route_lru_serves_repeat_request_without_upstream(monkeypatch):
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"AUDIO", headers={"content-type": "audio/mpeg"})

    vs = _vs(handler)
    c = _api_client(monkeypatch, vs)
    async with c:
        r1 = await c.post("/v1/tts/synthesize", json={"text": "lru duy nhat"})
        r2 = await c.post("/v1/tts/synthesize", json={"text": "lru duy nhat"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.content == b"AUDIO"
    assert calls == 1  # second request served from the in-process LRU


async def test_route_503_even_with_if_none_match_when_disabled(monkeypatch):
    vs = VoiceStudioClient(Settings(voicestudio_url=""), client=httpx.AsyncClient())
    c = _api_client(monkeypatch, vs, voicestudio_url="")
    async with c:
        resp = await c.post(
            "/v1/tts/synthesize", json={"text": "x"}, headers={"If-None-Match": '"abc"'}
        )
    assert resp.status_code == 503


async def test_pool_timeout_maps_to_503_overloaded():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.PoolTimeout("no conn", request=request)

    vs = _vs(handler)
    with pytest.raises(MetaSearchError) as exc:
        await vs.synthesize("x")
    assert exc.value.code == ErrorCode.OVERLOADED
    assert exc.value.status_code == 503


@pytest.mark.parametrize(
    "bad",
    [
        "http://host:3900/prefix",
        "http://[::1",
        "http://host?x=1#f",
        "http://host:abc",
        "http://host:99999",
        "HTTP://OK:3900",
    ],
)
def test_url_validation_edges(bad):
    # /prefix, bad ports and malformed URLs are rejected; plain http origin is fine
    vs = VoiceStudioClient(Settings(voicestudio_url=bad), client=httpx.AsyncClient())
    if bad == "HTTP://OK:3900":
        assert vs.enabled is True  # scheme case-insensitive per RFC 3986
    else:
        assert vs.enabled is False


def test_admission_has_tts_tier():
    from app.core.admission import _TIER_CONFIG

    assert _TIER_CONFIG["tts"] == (8, 1.0)


async def test_route_single_flight_coalesces_concurrent_identical_requests(monkeypatch):
    import asyncio

    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)  # hold the upstream so both requests overlap
        return httpx.Response(200, content=b"AUDIO", headers={"content-type": "audio/mpeg"})

    vs = _vs(handler)
    c = _api_client(monkeypatch, vs)
    async with c:
        r1, r2 = await asyncio.gather(
            c.post("/v1/tts/synthesize", json={"text": "single flight duy nhat"}),
            c.post("/v1/tts/synthesize", json={"text": "single flight duy nhat"}),
        )
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.content == r2.content == b"AUDIO"
    assert calls == 1  # coalesced by the single-flight, not by the LRU alone


# ------------------------------------------------- audit round-8 hardening


def test_etag_fields_are_length_prefixed_no_cross_field_collision():
    """(model="a|b", voice="c") must not collide with (model="a", voice="b|c")."""
    e1 = VoiceStudioClient.etag("a|b", "c", "mp3", "x")
    e2 = VoiceStudioClient.etag("a", "b|c", "mp3", "x")
    assert e1 != e2


async def test_upstream_redirect_is_never_followed_returns_502(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "http://evil.test/audio", "content-type": "audio/mpeg"},
        )

    vs = _vs(handler)
    with pytest.raises(MetaSearchError) as exc:
        await vs.synthesize("xin chào")
    assert exc.value.code == ErrorCode.TTS_UPSTREAM_ERROR


async def test_audio_lru_evicts_by_count_and_bytes(monkeypatch):
    from app.api.v1 import tts as tts_route

    def handler(request: httpx.Request) -> httpx.Response:
        json.loads(request.content)  # exercise the parse path, bytes are what matters
        return httpx.Response(
            200,
            content=b"A" * 4096,
            headers={"content-type": "audio/mpeg"},
        )

    vs = _vs(handler)
    async with _api_client(monkeypatch, vs) as client:
        for i in range(20):  # more than _AUDIO_LRU_MAX_ENTRIES (16)
            resp = await client.post(
                "/v1/tts/synthesize", json={"text": f"tin số {i} để đuổi cache"}
            )
            assert resp.status_code == 200
        assert len(tts_route._audio_lru) <= tts_route._AUDIO_LRU_MAX_ENTRIES
        assert tts_route._audio_lru_bytes <= tts_route._AUDIO_LRU_MAX_BYTES
        # Byte accounting must stay consistent with the stored entries.
        total = sum(len(audio) for audio, _ in tts_route._audio_lru.values())
        assert total == tts_route._audio_lru_bytes


def test_route_requires_api_key(monkeypatch):
    """AUTH_ENABLED=true must 401 both TTS routes (router-level _auth)."""
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import create_app

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("VOICESTUDIO_URL", "http://vs.test:3900")
    monkeypatch.setattr(deps, "get_tts_client", lambda: _vs(_settings()))
    get_settings.cache_clear()  # env changed: drop the cached settings singleton
    try:
        authed_app = create_app()
        client = TestClient(authed_app)
        caps = client.get("/v1/tts/capabilities")
        assert caps.status_code in (401, 403)
        syn = client.post("/v1/tts/synthesize", json={"text": "x"})
        assert syn.status_code in (401, 403)
    finally:
        get_settings.cache_clear()  # env reverted: let other tests recompute
