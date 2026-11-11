"""TTS endpoints: optional server-side VoiceStudio synthesis (docs/17).

The browser Web Speech API stays the default (zero backend dependency); these
routes exist only for deployments that run a local VoiceStudio server and set
VOICESTUDIO_URL. Binary audio responses carry an ETag (sha256 of
model|voice|format|text) so API consumers can revalidate with 304; identical
concurrent requests are coalesced (single-flight) and recent audio is kept in
a small bounded in-process LRU keyed by that ETag.
"""

from __future__ import annotations

from collections import OrderedDict

from fastapi import APIRouter, Request, Response

from app.api import deps
from app.api.v1.schemas import TtsSynthesizeRequest
from app.config import get_settings
from app.core.admission import admit
from app.core.errors import ErrorCode, MetaSearchError
from app.core.singleflight import SingleFlight

router = APIRouter(tags=["tts"])

# Audio is derived from the caller's text: never let a shared cache store it.
# (RFC 9111: "public" would explicitly allow that even on authorized requests.)
_TTS_CACHE_CONTROL = "private, max-age=3600"


def reset_tts_state() -> None:
    """Clear the in-process audio LRU (test isolation; settings changes)."""
    global _audio_lru_bytes
    _audio_lru.clear()
    _audio_lru_bytes = 0


_tts_flight = SingleFlight()
# Bounded in-process LRU for synthesized audio (keyed by ETag). Caps by both
# entry count and total bytes so a Lite-profile host cannot be OOM-ed.
_audio_lru: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
_AUDIO_LRU_MAX_ENTRIES = 16
_AUDIO_LRU_MAX_BYTES = 32 * 1024 * 1024
_audio_lru_bytes = 0


def _store_audio(etag: str, audio: bytes, media_type: str) -> None:
    global _audio_lru_bytes
    if etag in _audio_lru:
        _audio_lru_bytes -= len(_audio_lru.pop(etag)[0])
    _audio_lru[etag] = (audio, media_type)
    _audio_lru_bytes += len(audio)
    _audio_lru.move_to_end(etag)
    while _audio_lru and (
        _audio_lru_bytes > _AUDIO_LRU_MAX_BYTES or len(_audio_lru) > _AUDIO_LRU_MAX_ENTRIES
    ):
        _old_key, (old_audio, _old_media) = _audio_lru.popitem(last=False)
        _audio_lru_bytes -= len(old_audio)


@router.get("/tts/capabilities")
async def capabilities() -> dict:
    """Which server-side TTS engines are configured (no secrets, no URL)."""
    return deps.get_tts_client().capabilities()


@router.post("/tts/synthesize", response_class=Response)
async def synthesize(http_request: Request, payload: TtsSynthesizeRequest) -> Response:
    settings = get_settings()
    if len(payload.text) > settings.tts_max_chars:
        raise MetaSearchError(
            ErrorCode.INVALID_REQUEST,
            f"text is {len(payload.text)} chars, tts_max_chars={settings.tts_max_chars}",
            status_code=400,
        )
    client = deps.get_tts_client()
    if not client.enabled:
        raise MetaSearchError(
            ErrorCode.TTS_DISABLED,
            "Server-side TTS is not configured (VOICESTUDIO_URL empty); "
            "the Web UI falls back to the browser's Web Speech API",
            status_code=503,
        )
    etag = client.etag(
        payload.model or settings.voicestudio_model,
        payload.voice if payload.voice is not None else settings.voicestudio_voice,
        payload.response_format,
        payload.text,
    )
    headers = {"ETag": etag, "Cache-Control": _TTS_CACHE_CONTROL}
    # Custom revalidation for API consumers: exact-match If-None-Match on this
    # POST returns 304 (RFC 9110 would say 412 for non-GET; we keep 304 because
    # the browser UI never sends it and 304 avoids re-sending audio bytes).
    if http_request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    hit = _audio_lru.get(etag)
    if hit is not None:
        _audio_lru.move_to_end(etag)
        audio, media_type = hit
    else:

        async def _synthesize_admitted() -> tuple[bytes, str]:
            # Admission is taken INSIDE the flight: coalesced waiters of one
            # identical request share the leader's slot instead of each burning
            # an admit("tts") permit for an upstream call that never happens.
            async with admit("tts"):
                return await client.synthesize(
                    payload.text,
                    voice=payload.voice,
                    model=payload.model,
                    response_format=payload.response_format,
                )

        audio, media_type = await _tts_flight.run(etag, _synthesize_admitted)
        _store_audio(etag, audio, media_type)
    return Response(content=audio, media_type=media_type, headers=headers)
