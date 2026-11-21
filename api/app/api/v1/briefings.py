"""Expert Briefing endpoints (docs/33).

GET  /v1/briefings/channels         -> registry introspection (window/persona/tags)
POST /v1/briefings/{channel_id}     -> generate one expert briefing now
                                       (deterministic evidence pipeline -> Step A
                                       persona synthesis -> grounding verifier ->
                                       Step B council review -> persist BlogPost)

Briefings persist as BlogPost rows (topic = channel id, tags carry the persona)
so the existing Blog Chuyên Sâu tab, /v1/digests and the podcast audio pipeline
consume them unchanged (docs/33 §4 audio enablement).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Path

from app.api import deps
from app.api.v1.digests import (
    BlogPostDetail,  # shared blog contract (Summary unused: POST returns full detail)
)
from app.config import get_settings
from app.core.auth import require_admin
from app.core.errors import ErrorCode, MetaSearchError
from app.core.logging import get_logger
from app.db.models import BlogPost as BlogPostModel
from app.db.session import sqlite_write_lock
from app.orchestration.briefing_engine import BriefingEngine
from app.orchestration.briefing_registry import (
    channel_to_topic_view,
    load_channel_overrides,
)

logger = get_logger(__name__)

router = APIRouter(tags=["briefings"])

# Per-channel single-flight: two admins pressing "generate" share one run.
_active: dict[str, asyncio.Task[Any]] = {}


def _resolve_channels() -> dict[str, Any]:
    settings = get_settings()
    override = str(getattr(settings, "briefing_channels_file", "") or "")
    return load_channel_overrides(override or None)


def _engine() -> BriefingEngine:
    return BriefingEngine(deps.get_registry(), deps.get_gateway())


def _persist(post: BlogPostModel) -> None:
    """Best-effort background persist — never blocks the response (docs/33)."""
    from app.api.deps import get_session_factory

    async def _write() -> None:
        async with sqlite_write_lock(), get_session_factory()() as session:
            session.add(post)
            await session.commit()

    try:
        task = asyncio.create_task(_write())
        _background_persist.add(task)

        def _log_persist_error(done: asyncio.Task[None]) -> None:
            _background_persist.discard(done)
            exc = done.exception()
            if exc is not None:
                logger.warning("briefing_persist_write_failed", error=type(exc).__name__)

        task.add_done_callback(_log_persist_error)
    except Exception as exc:
        logger.warning("briefing_persist_failed", error=type(exc).__name__)


_background_persist: set[asyncio.Task[None]] = set()


def _post_from_result(result: Any, now: datetime) -> tuple[BlogPostModel, BlogPostDetail]:
    post = BlogPostModel(
        id=uuid.uuid4(),
        slug=f"{result.channel_id}-{now.strftime('%Y-%m-%d')}-{uuid.uuid4().hex[:8]}",
        topic=result.channel_id,
        title=result.title,
        summary=result.summary,
        content_md=result.content_md,
        tags=_tags_for(result),
        sources=result.citations,
        published_at=now,
    )
    detail = BlogPostDetail(
        id=str(post.id),
        slug=post.slug,
        topic=post.topic,
        title=post.title,
        summary=post.summary,
        content_md=post.content_md,
        tags=post.tags,
        sources=post.sources,
        published_at=now.isoformat(),
        created_at=now.isoformat(),
    )
    return post, detail


def _tags_for(result: Any) -> list[str]:
    settings = get_settings()
    channels = _resolve_channels()
    ch = channels.get(result.channel_id)
    tags = list(ch.tags) if ch else [result.channel_id]
    tags.extend(["ai-briefing"] if result.ai_grounded else ["extractive-briefing"])
    if getattr(settings, "briefing_review_enabled", True) and result.review_verdicts:
        tags.append("council-reviewed")
    return tags[:12]


@router.get("/briefings/channels")
async def list_channels() -> dict[str, Any]:
    """Registry introspection: every channel's window, persona and tags."""
    settings = get_settings()
    override = str(getattr(settings, "briefing_channels_file", "") or "")
    channels = load_channel_overrides(override or None)
    news = [
        channel_to_topic_view(ch) for ch in channels.values() if ch.kind == "news" and ch.enabled
    ]
    deep = [
        channel_to_topic_view(ch) for ch in channels.values() if ch.kind == "deep" and ch.enabled
    ]
    return {
        "news_channels": news,
        "deep_channels": deep,
        "ai_enabled": bool(getattr(settings, "briefing_ai_enabled", True)),
        "review_enabled": bool(getattr(settings, "briefing_review_enabled", True)),
        # docs/33 audit P3-10: the operator file path is server-side config —
        # never echoed to callers.
        "override_file_configured": bool(override),
    }


@router.post(
    "/briefings/{channel_id}",
    response_model=BlogPostDetail,
    dependencies=[Depends(require_admin)],
)
async def generate_briefing(
    channel_id: str = Path(min_length=1, max_length=64),
) -> BlogPostDetail:
    """Generate one expert briefing NOW for the given channel (deterministic
    evidence pipeline -> persona synthesis -> grounding verifier -> council
    review). Admin-gated like the scanner; per-channel single-flight."""
    settings = get_settings()
    channels = _resolve_channels()
    channel = channels.get(channel_id)
    if channel is None or not channel.enabled:
        known = ", ".join(sorted(channels.keys()))
        raise MetaSearchError(
            ErrorCode.INVALID_REQUEST,
            f"Unknown briefing channel: {channel_id!r}. Valid channels: {known}",
            status_code=400,
        )

    existing = _active.get(channel_id)
    if existing is not None and not existing.done():
        raise MetaSearchError(
            ErrorCode.OVERLOADED,
            f"Briefing for {channel_id} is already being generated",
            status_code=429,
            headers={"Retry-After": "20"},
        )

    engine = _engine()
    now = datetime.now(UTC)

    async def _run() -> Any:
        return await engine.generate(
            channel,
            now=now,
            use_ai=bool(getattr(settings, "briefing_ai_enabled", True)),
        )

    task = asyncio.create_task(_run())
    _active[channel_id] = task
    # docs/33 audit P2-4: identity-guarded cleanup — the entry must stay in
    # _active while the (shielded) generation is still running, so a client
    # cancellation cannot orphan a run and let a second one start concurrently;
    # and it must never pop a successor's task.
    def _release(done: asyncio.Task[Any], *, cid: str = channel_id) -> None:
        if _active.get(cid) is done:
            _active.pop(cid, None)

    task.add_done_callback(_release)
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        raise
    except MetaSearchError:
        raise
    except Exception as exc:
        logger.warning("briefing_generate_failed", channel=channel_id, error=type(exc).__name__)
        raise MetaSearchError(
            ErrorCode.INTERNAL, "briefing generation failed", status_code=500
        ) from exc

    if not result.citations:
        raise MetaSearchError(
            ErrorCode.NO_RESULTS,
            f"Không đủ bài viết trong cửa sổ {channel.window_hours}h cho kênh {channel.name}",
            status_code=503,
            details={"channel": channel_id, "window_hours": channel.window_hours},
        )

    post, detail = _post_from_result(result, now)
    _persist(post)
    # Full detail (content_md + sources) — the frontend renders it directly.
    return detail


@router.get("/briefings/{channel_id}/latest", response_model=BlogPostDetail)
async def latest_briefing(
    channel_id: str = Path(min_length=1, max_length=64),
) -> BlogPostDetail:
    """Newest persisted briefing for the channel (DB first, memory fallback)."""
    channels = _resolve_channels()
    channel = channels.get(channel_id)
    if channel is None or not channel.enabled:
        raise MetaSearchError(
            ErrorCode.INVALID_REQUEST, f"Unknown briefing channel: {channel_id!r}", status_code=400
        )
    try:
        from sqlalchemy import select as _select

        from app.db.models import BlogPost as BlogPostModel

        async with deps.get_session_factory()() as session:
            # docs/33 audit P3-7: the blog table is shared with the scanner's
            # extractive digests — prefer the newest row that is actually an
            # AI briefing (tag discriminator) so /latest never serves a
            # scanner post as this channel's expert briefing.
            rows = (
                (
                    await session.execute(
                        _select(BlogPostModel)
                        .where(BlogPostModel.topic == channel_id)
                        .order_by(BlogPostModel.published_at.desc())
                        .limit(8)
                    )
                )
                .scalars()
                .all()
            )
    except Exception as exc:
        # A real DB outage must not masquerade as "no briefing yet".
        logger.warning("briefing_latest_db_failed", error=type(exc).__name__)
        raise MetaSearchError(
            ErrorCode.INTERNAL, "briefing store unavailable", status_code=500
        ) from exc
    for row in rows:
        if "ai-briefing" in (row.tags or []):
            return BlogPostDetail(
                id=str(row.id),
                slug=row.slug,
                topic=row.topic,
                title=row.title,
                summary=row.summary,
                content_md=row.content_md,
                tags=row.tags,
                sources=row.sources,
                published_at=row.published_at.isoformat() if row.published_at else None,
                created_at=row.created_at.isoformat() if row.created_at else None,
            )
    raise MetaSearchError(
        ErrorCode.NO_RESULTS,
        f"no persisted AI briefing for {channel_id} yet — generate one first",
        status_code=404,
    )
