"""API key authentication + Redis sliding-window rate limit (docs/01 FR-14).

Design:
- Keys are generated as ``msk_<32 hex>``, stored as sha256 hashes with a
  short ``key_prefix`` for identification (sha256 is appropriate: keys are
  high-entropy secrets, not passwords).
- ``AUTH_ENABLED=false`` (default) keeps local dev and the Web UI open —
  enforcement is opt-in for exposed deployments; when enabled, every /v1
  request must carry ``Authorization: Bearer msk_...``.
- Rate limit: sliding window per key over Redis (ZSET). Redis unavailable ->
  limit enforced via bounded in-process sliding window — availability beats
  strictness for cross-worker sharing, while authentication itself fails closed
  (a dead DB with auth on returns 401 rather than letting traffic through).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import secrets
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from sqlalchemy import select

from app.cache import get_redis, mark_redis_down
from app.config import get_settings
from app.core.errors import ErrorCode, MetaSearchError
from app.core.logging import get_logger
from app.db.models import ApiKey as ApiKeyRow
from app.db.session import get_sessionmaker

logger = get_logger(__name__)

_KEY_PREFIX_LEN = 12  # "msk_" + 8 hex
_window_warned = False


@dataclass(slots=True)
class AuthContext:
    key_id: str
    name: str | None
    role: str
    rate_limit_rpm: int


def generate_key() -> tuple[str, str, str]:
    """Return (plaintext_key, sha256_hash, key_prefix)."""
    plaintext = f"msk_{secrets.token_hex(16)}"
    return plaintext, hash_key(plaintext), plaintext[:_KEY_PREFIX_LEN]


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()  # nosec B324


async def create_key(name: str, *, role: str = "user", rate_limit_rpm: int = 60) -> tuple[str, str]:
    """Create an API key; returns (plaintext, key_id)."""
    plaintext, key_hash, key_prefix = generate_key()
    async with get_sessionmaker()() as session:
        session.add(
            ApiKeyRow(
                key_hash=key_hash,
                key_prefix=key_prefix,
                name=name,
                role=role,
                rate_limit_rpm=rate_limit_rpm,
                enabled=True,
            )
        )
        await session.commit()
    return plaintext, key_prefix


_key_cache: dict[str, tuple[AuthContext | None, float]] = {}
_key_cache_lock = asyncio.Lock()
_CACHE_TTL_S = 60.0
_KEY_CACHE_MAX = 2048


async def authenticate(bearer: str | None) -> AuthContext:
    """Resolve the Bearer key to an AuthContext; 401 when invalid/disabled."""
    settings = get_settings()
    if not bearer or not bearer.startswith("msk_"):
        raise _unauthorized("missing or malformed API key")
    key_hash = hash_key(bearer)

    cached = _key_cache.get(key_hash)
    now = time.monotonic()
    if cached and now - cached[1] < _CACHE_TTL_S:
        if cached[0] is None:
            raise _unauthorized("unknown API key")
        return cached[0]

    try:
        async with get_sessionmaker()() as session:
            row = (
                await session.execute(
                    select(ApiKeyRow).where(
                        ApiKeyRow.key_hash == key_hash, ApiKeyRow.enabled.is_(True)
                    )
                )
            ).scalar_one_or_none()
    except Exception as exc:
        # Auth enabled + DB unavailable -> fail closed, never open.
        logger.warning("auth_db_unavailable", error=type(exc).__name__)
        raise _unauthorized("authentication backend unavailable") from exc

    if row is None:
        await _cache_put(key_hash, None)
        raise _unauthorized("unknown API key")
    ctx = AuthContext(
        key_id=str(row.id),
        name=row.name,
        role=row.role or "user",
        rate_limit_rpm=int(row.rate_limit_rpm or settings.rate_limit_rpm),
    )
    await _cache_put(key_hash, ctx)
    return ctx


async def _cache_put(key_hash: str, ctx: AuthContext | None) -> None:
    now = time.monotonic()
    async with _key_cache_lock:
        if len(_key_cache) >= _KEY_CACHE_MAX:
            expired = [k for k, (_, ts) in _key_cache.items() if now - ts >= _CACHE_TTL_S]
            for k in expired:
                _key_cache.pop(k, None)
            if len(_key_cache) >= _KEY_CACHE_MAX:
                _key_cache.pop(next(iter(_key_cache)), None)
        _key_cache[key_hash] = (ctx, now)


def _unauthorized(message: str) -> MetaSearchError:
    return MetaSearchError(ErrorCode.UNAUTHORIZED, message)


# -- sliding window rate limit -------------------------------------------------


_local_windows: dict[str, deque[float]] = {}
_LOCAL_WINDOW_MAX = 10_000


def _local_rate_limit(ctx: AuthContext, *, limit: int, window_s: int) -> tuple[bool, int]:
    """In-process sliding window — Lite profile / Redis-down fallback.

    Per-worker only (multi-worker deployments should run Redis); bounded to
    keep memory flat under abuse.
    """
    now = time.monotonic()
    hits = _local_windows.setdefault(ctx.key_id, deque())
    cutoff = now - window_s
    while hits and hits[0] < cutoff:
        hits.popleft()
    if len(hits) >= limit:
        return False, 0
    hits.append(now)
    remaining = max(limit - len(hits), 0)
    if len(_local_windows) > _LOCAL_WINDOW_MAX:
        _local_windows.clear()
    return True, remaining


async def check_rate_limit(ctx: AuthContext, *, window_s: int = 60) -> dict[str, Any]:
    """Sliding-window rate limit: Redis first, in-process fallback second.

    Both paths enforce the limit (the Lite profile keeps protection without
    Redis); they differ only in whether the window is shared across workers.
    """
    global _window_warned
    limit = max(ctx.rate_limit_rpm, 1)
    headers = {"X-RateLimit-Limit": str(limit)}
    client = get_redis()
    if client is None:
        ok, remaining = _local_rate_limit(ctx, limit=limit, window_s=window_s)
        headers["X-RateLimit-Remaining"] = str(remaining)
        if not ok:
            headers["Retry-After"] = str(window_s)
            raise MetaSearchError(
                ErrorCode.RATE_LIMITED,
                "rate limit exceeded",
                details={"limit": limit, "window_s": window_s, "backend": "local"},
                headers=headers,
            ) from None
        return headers
    try:
        now_ms = int(time.time() * 1000)
        window_ms = window_s * 1000
        member = f"{now_ms}:{secrets.token_hex(4)}"
        key = f"rl:{ctx.key_id}"
        async with client.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(key, 0, now_ms - window_ms)
            pipe.zadd(key, {member: now_ms})
            pipe.zcard(key)
            pipe.expire(key, window_s + 1)
            results = await pipe.execute()
        count = int(results[2] or 0)
        headers["X-RateLimit-Remaining"] = str(max(limit - count, 0))
        if count > limit:
            headers["Retry-After"] = str(window_s)
            raise MetaSearchError(
                ErrorCode.RATE_LIMITED,
                "rate limit exceeded",
                details={"limit": limit, "window_s": window_s},
                headers=headers,
            )
        return headers
    except MetaSearchError:
        raise
    except Exception as exc:
        mark_redis_down()
        if not _window_warned:
            logger.warning("rate_limit_redis_unavailable", error=type(exc).__name__)
            _window_warned = True
        # Redis broke mid-request: the local window keeps enforcement on.
        ok, remaining = _local_rate_limit(ctx, limit=limit, window_s=window_s)
        headers["X-RateLimit-Remaining"] = str(remaining)
        if not ok:
            headers["Retry-After"] = str(window_s)
            raise MetaSearchError(
                ErrorCode.RATE_LIMITED,
                "rate limit exceeded",
                details={"limit": limit, "window_s": window_s, "backend": "local"},
                headers=headers,
            ) from exc
        return headers


async def require_api_key(request: Request) -> AuthContext:
    """FastAPI dependency: authenticate + rate limit when AUTH_ENABLED=true."""
    settings = get_settings()
    if not settings.auth_enabled:
        return AuthContext(key_id="anonymous", name=None, role="admin", rate_limit_rpm=0)
    raw_auth = request.headers.get("authorization", "").strip()
    bearer = raw_auth[7:].strip() if raw_auth[:7].lower() == "bearer " else raw_auth
    ctx = await authenticate(bearer)
    rl_headers = await check_rate_limit(ctx)
    state = getattr(request, "state", None)
    if state is not None:
        with contextlib.suppress(Exception):
            state.rate_limit_headers = rl_headers
    return ctx


async def require_admin(request: Request) -> AuthContext:
    """FastAPI dependency: require an admin role when AUTH_ENABLED=true."""
    ctx = await require_api_key(request)
    if ctx.role != "admin":
        raise MetaSearchError(
            ErrorCode.UNAUTHORIZED,
            "admin role required for this operation",
            status_code=403,
        )
    return ctx
