"""Optional server-side TTS via a local VoiceStudio instance (docs/17).

VoiceStudio (github.com/debpalash/VoiceStudio) exposes an OpenAI-compatible
local speech platform (default http://127.0.0.1:3900): ``POST /v1/audio/speech``
for TTS. We integrate over HTTP only — a separate process, no code import —
so the AGPL-3.0 app license does not reach this codebase (docs/17 §licensing).
The base URL comes from env config only, never from a request (SSRF rule).
"""

from __future__ import annotations

import asyncio
import hashlib
from urllib.parse import urlsplit

import httpx

from app.config import Settings
from app.core.errors import ErrorCode, MetaSearchError
from app.http import POOL_ACQUIRE_TIMEOUT_S, get_http_client

_ALLOWED_FORMATS = {"mp3", "wav", "opus", "flac", "pcm"}
_MEDIA_TYPES = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "opus": "audio/ogg",
    "flac": "audio/flac",
    "pcm": "audio/L16",
}
# Upstream content-type is only trusted if it is a known audio type — a
# hostile upstream must not smuggle header values into our response.
_TRUSTED_MEDIA_TYPES = frozenset(_MEDIA_TYPES.values()) | {"audio/x-wav", "audio/wave"}
_ERROR_BODY_CAP = 64 * 1024


class VoiceStudioClient:
    """Thin proxy to a configured VoiceStudio server; disabled when no URL."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._base_url = self._validate_url(settings.voicestudio_url)
        self._client = client

    @staticmethod
    def _validate_url(url: str) -> str | None:
        # Env-only config, but still reject anything that is not a plain
        # http(s) origin — a request must never influence this value.
        if not url:
            return None
        try:
            parts = urlsplit(url.strip())
            _ = parts.port  # raises ValueError on bad/out-of-range port
        except ValueError:
            return None  # malformed URL (bad IPv6 literal, bad port) = disabled
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        if parts.username or parts.password or parts.query or parts.fragment:
            return None
        if parts.path not in ("", "/"):
            return None  # origin-only: a path prefix would silently 404 upstream
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def enabled(self) -> bool:
        return self._base_url is not None

    def capabilities(self) -> dict:
        s = self._settings
        return {
            "voicestudio": {
                "enabled": self.enabled,
                "model": s.voicestudio_model if self.enabled else None,
                "voice": (s.voicestudio_voice or None) if self.enabled else None,
            }
        }

    @staticmethod
    def etag(model: str, voice: str, response_format: str, text: str) -> str:
        # Length-prefixed fields (audit): user-controlled model/voice may contain
        # '|', so a plain join would let (model="a|b", voice="c") collide with
        # (model="a", voice="b|c") — two different payloads sharing one cache key.
        def part(value: str) -> str:
            return f"{len(value)}:{value}"

        digest = hashlib.sha256(
            "|".join([part(model), part(voice), part(response_format), part(text)]).encode()
        ).hexdigest()[:32]
        return f'"{digest}"'

    async def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        model: str | None = None,
        response_format: str = "mp3",
    ) -> tuple[bytes, str]:
        s = self._settings
        if not self.enabled:
            raise MetaSearchError(
                ErrorCode.TTS_DISABLED,
                "Server-side TTS is not configured (VOICESTUDIO_URL empty); "
                "the Web UI falls back to the browser's Web Speech API",
                status_code=503,
            )
        fmt = response_format if response_format in _ALLOWED_FORMATS else "mp3"
        model = model or s.voicestudio_model
        voice = voice if voice is not None else s.voicestudio_voice
        payload: dict = {"model": model, "input": text, "response_format": fmt}
        if voice:
            payload["voice"] = voice
        headers = {}
        if s.voicestudio_api_key:
            headers["Authorization"] = f"Bearer {s.voicestudio_api_key}"
        client = self._client or get_http_client("tts")
        url = f"{self._base_url}/v1/audio/speech"
        # Explicit per-phase timeout: a scalar timeout= would also raise the
        # pool-acquire timeout from POOL_ACQUIRE_TIMEOUT_S to voicestudio_timeout_s,
        # letting requests queue for 30s instead of failing fast on congestion.
        timeout = httpx.Timeout(
            connect=5.0,
            read=s.voicestudio_timeout_s,
            write=s.voicestudio_timeout_s,
            pool=POOL_ACQUIRE_TIMEOUT_S,
        )
        try:
            # Wall-clock deadline for the whole upstream call: the httpx timeout
            # is per-read, so a trickling upstream would otherwise never trip it.
            async with asyncio.timeout(s.voicestudio_timeout_s):
                # Stream and cap: never buffer an unbounded upstream body.
                async with client.stream(
                    "POST", url, json=payload, headers=headers, timeout=timeout
                ) as resp:
                    if resp.status_code != 200:
                        # Cap the error body too — a failure must not make us
                        # buffer gigabytes either.
                        err_size = 0
                        async for chunk in resp.aiter_bytes():
                            err_size += len(chunk)
                            if err_size > _ERROR_BODY_CAP:
                                break
                        raise MetaSearchError(
                            ErrorCode.TTS_UPSTREAM_ERROR,
                            f"VoiceStudio TTS returned HTTP {resp.status_code}",
                            status_code=502,
                            details={"upstream_status": resp.status_code},
                        )
                    # bytearray.extend avoids the second full copy that
                    # b"".join(chunks) would make at the cap boundary.
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        if len(buf) + len(chunk) > s.tts_max_audio_bytes:
                            raise MetaSearchError(
                                ErrorCode.TTS_UPSTREAM_ERROR,
                                "VoiceStudio TTS audio exceeds tts_max_audio_bytes",
                                status_code=502,
                                details={"max_bytes": s.tts_max_audio_bytes},
                            )
                        buf.extend(chunk)
                    upstream_media = (
                        resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    )
                    media = (
                        upstream_media
                        if upstream_media in _TRUSTED_MEDIA_TYPES
                        else _MEDIA_TYPES.get(fmt, "application/octet-stream")
                    )
                    audio = bytes(buf)
        except httpx.PoolTimeout as exc:
            # Local pool congestion (our own limit), not an upstream timeout.
            raise MetaSearchError(
                ErrorCode.OVERLOADED,
                "TTS connection pool saturated",
                status_code=503,
                headers={"Retry-After": "1"},
            ) from exc
        except httpx.TimeoutException as exc:
            raise MetaSearchError(
                ErrorCode.UPSTREAM_TIMEOUT,
                f"VoiceStudio TTS timeout after {s.voicestudio_timeout_s}s",
                status_code=504,
            ) from exc
        except TimeoutError as exc:  # asyncio.timeout wall-clock deadline
            raise MetaSearchError(
                ErrorCode.UPSTREAM_TIMEOUT,
                f"VoiceStudio TTS exceeded wall-clock budget {s.voicestudio_timeout_s}s",
                status_code=504,
            ) from exc
        except httpx.RequestError as exc:
            raise MetaSearchError(
                ErrorCode.TTS_UPSTREAM_ERROR,
                f"VoiceStudio TTS unreachable: {type(exc).__name__}",
                status_code=502,
            ) from exc
        return audio, media
