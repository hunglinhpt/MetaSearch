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


def reset_rate_limit_windows() -> None:
    """Clear in-memory sliding windows (for test isolation)."""
    _local_windows.clear()


def _local_rate_limit(
    ctx: AuthContext, *, cost: int = 1, limit: int, window_s: int
) -> tuple[bool, int, int]:
    """In-process sliding window — Lite profile / Redis-down fallback.

    Per-worker only (multi-worker deployments should run Redis); bounded to
    keep memory flat under abuse.
    Returns (ok, remaining, retry_after_s).
    """
    now = time.monotonic()
    raw_hits = _local_windows.setdefault(ctx.key_id, deque())
    cutoff = now - window_s
    while raw_hits:
        first = raw_hits[0]
        ts = first[0] if isinstance(first, tuple) else first
        if ts < cutoff:
            raw_hits.popleft()
        else:
            break

    current_weight = sum(item[1] if isinstance(item, tuple) else 1 for item in raw_hits)
    if current_weight + cost > limit:
        first_hit = raw_hits[0] if raw_hits else now
        oldest_ts = first_hit[0] if isinstance(first_hit, tuple) else first_hit
        retry_after = max(1, int(oldest_ts + window_s - now + 0.999))
        return False, max(limit - current_weight, 0), retry_after

    raw_hits.append((now, cost))
    remaining = max(limit - (current_weight + cost), 0)
    if len(_local_windows) > _LOCAL_WINDOW_MAX:
        _local_windows.clear()
    return True, remaining, 0


async def check_rate_limit(
    ctx: AuthContext, *, cost: int = 1, window_s: int = 60
) -> dict[str, Any]:
    """Sliding-window rate limit: Redis first, in-process fallback second.

    Supports weighted request costs (e.g. answer=5, context=3, search=1, research=20).
    Calculates dynamic Retry-After from the oldest active entry.
    """
    global _window_warned
    limit = max(ctx.rate_limit_rpm, 1)
    headers = {"X-RateLimit-Limit": str(limit)}
    client = get_redis()
    if client is None:
        ok, remaining, retry_after = _local_rate_limit(
            ctx, cost=cost, limit=limit, window_s=window_s
        )
        headers["X-RateLimit-Remaining"] = str(remaining)
        if not ok:
            headers["Retry-After"] = str(retry_after)
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
        key = f"rl:{ctx.key_id}"
        async with client.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(key, 0, now_ms - window_ms)
            for i in range(cost):
                pipe.zadd(key, {f"{now_ms}:{secrets.token_hex(4)}:{i}": now_ms})
            pipe.zcard(key)
            has_zrange = hasattr(pipe, "zrange")
            if has_zrange:
                pipe.zrange(key, 0, 0, withscores=True)
            pipe.expire(key, window_s + 1)
            results = await pipe.execute()
        count = int(results[1 + cost] or 0)
        oldest_raw = results[2 + cost] if (has_zrange and len(results) > (2 + cost)) else None
        oldest_items = oldest_raw if isinstance(oldest_raw, (list, tuple)) else []
        if oldest_items and isinstance(oldest_items[0], (list, tuple)) and len(oldest_items[0]) > 1:
            oldest_ms = oldest_items[0][1]
            retry_after = max(1, int((oldest_ms + window_ms - now_ms) / 1000.0 + 0.999))
        else:
            retry_after = window_s
        headers["X-RateLimit-Remaining"] = str(max(limit - count, 0))
        if count > limit:
            headers["Retry-After"] = str(retry_after)
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
        ok, remaining, retry_after = _local_rate_limit(
            ctx, cost=cost, limit=limit, window_s=window_s
        )
        headers["X-RateLimit-Remaining"] = str(remaining)
        if not ok:
            headers["Retry-After"] = str(retry_after)
            raise MetaSearchError(
                ErrorCode.RATE_LIMITED,
                "rate limit exceeded",
                details={"limit": limit, "window_s": window_s, "backend": "local"},
                headers=headers,
            ) from exc
        return headers


async def authenticate_and_rate_limit(request: Request, *, cost: int | None = None) -> AuthContext:
    """Core authenticator with weighted rate limit check.

    If cost is not explicitly provided, applies route weights per WP-07:
    answer=5, context=3, research=20, search/default=1.
    """
    settings = get_settings()
    if not settings.auth_enabled:
        return AuthContext(key_id="anonymous", name=None, role="admin", rate_limit_rpm=0)

    if cost is None:
        path = request.url.path.rstrip("/")
        if path.endswith("/answer"):
            cost = 5
        elif path.endswith("/context"):
            cost = 3
        elif path.endswith("/research") and request.method == "POST":
            cost = 20
        else:
            cost = 1

    raw_auth = request.headers.get("authorization", "").strip()
    bearer = raw_auth[7:].strip() if raw_auth[:7].lower() == "bearer " else raw_auth
    ctx = await authenticate(bearer)
    rl_headers = await check_rate_limit(ctx, cost=cost)
    state = getattr(request, "state", None)
    if state is not None:
        with contextlib.suppress(Exception):
            state.rate_limit_headers = rl_headers
    return ctx


async def require_api_key(request: Request) -> AuthContext:
    """FastAPI dependency: authenticate + rate limit when AUTH_ENABLED=true."""
    return await authenticate_and_rate_limit(request)


def require_weighted_key(cost: int):
    """FastAPI dependency factory for weighted rate limiting."""

    async def _dependency(request: Request) -> AuthContext:
        return await authenticate_and_rate_limit(request, cost=cost)

    return _dependency


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
