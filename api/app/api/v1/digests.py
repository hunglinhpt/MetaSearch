"""Daily briefings & blog posts API endpoints (docs/09-daily-digests).

Exposes:
- GET /api/v1/digests: List blog posts (filterable by topic, with pagination)
- GET /api/v1/digests/topics: List configured scan topics & scheduler status
- GET /api/v1/digests/{slug_or_id}: Retrieve single blog post
- POST /api/v1/digests/scan: Trigger on-demand automated scan for a topic or all topics
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from app.api import deps
from app.core.auth import require_admin
from app.core.errors import ErrorCode, MetaSearchError
from app.core.logging import get_logger
from app.db.models import BlogPost

logger = get_logger(__name__)

router = APIRouter(prefix="/digests", tags=["digests"])


class TriggerScanRequest(BaseModel):
    topic: str | None = Field(
        default=None,
        description=(
            "Topic ID to scan ('ai_coding', 'ai_agents', 'viet_finance', "
            "'macro_business', 'fengshui_lifestyle', 'health_wellness'). "
            "If omitted, scans all topics."
        ),
    )


class BlogPostSummary(BaseModel):
    id: str
    slug: str
    topic: str
    title: str
    summary: str | None
    tags: list[str] | None
    published_at: str | None
    created_at: str | None


class BlogPostDetail(BlogPostSummary):
    content_md: str
    sources: list[dict[str, Any]] | None


class ScanTopicInfo(BaseModel):
    id: str
    name: str
    query: str
    category: str
    tags: list[str]
    description: str
    last_run: str | None
    last_post_id: str | None


def _to_summary(r: Any) -> BlogPostSummary:
    return BlogPostSummary(
        id=str(r.id),
        slug=r.slug,
        topic=r.topic,
        title=r.title,
        summary=r.summary,
        tags=r.tags,
        published_at=r.published_at.isoformat() if r.published_at else None,
        created_at=r.created_at.isoformat() if getattr(r, "created_at", None) else None,
    )


def _to_detail(r: Any) -> BlogPostDetail:
    return BlogPostDetail(
        id=str(r.id),
        slug=r.slug,
        topic=r.topic,
        title=r.title,
        summary=r.summary,
        content_md=r.content_md,
        tags=r.tags,
        sources=r.sources,
        published_at=r.published_at.isoformat() if r.published_at else None,
        created_at=r.created_at.isoformat() if getattr(r, "created_at", None) else None,
    )


@router.get("/topics", response_model=list[ScanTopicInfo])
async def list_topics() -> list[ScanTopicInfo]:
    """List all available automated scanning topics and their latest scan state."""
    scanner = deps.get_scanner()
    out: list[ScanTopicInfo] = []
    for t in scanner.topics.values():
        out.append(
            ScanTopicInfo(
                id=t.id,
                name=t.name,
                query=t.query,
                category=t.category,
                tags=t.tags,
                description=t.description,
                last_run=t.last_run.isoformat() if t.last_run else None,
                last_post_id=t.last_post_id,
            )
        )
    return out


@router.get("", response_model=list[BlogPostSummary])
async def list_digests(
    topic: str | None = Query(default=None, description="Filter by topic"),
    limit: int = Query(default=10, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
) -> list[BlogPostSummary]:
    """List published blog posts / daily briefings (with graceful memory fallback)."""
    try:
        async with deps.get_session_factory()() as session:
            stmt = select(BlogPost).order_by(desc(BlogPost.created_at)).offset(offset).limit(limit)
            if topic:
                stmt = stmt.where(BlogPost.topic == topic)

            result = await session.execute(stmt)
            rows = result.scalars().all()
            if rows:
                return [_to_summary(r) for r in rows]
    except Exception as exc:
        logger.warning("digests_list_db_unavailable", error=type(exc).__name__)

    scanner = deps.get_scanner()
    mem_posts = scanner.list_memory_posts(topic=topic, limit=limit, offset=offset)
    return [_to_summary(r) for r in mem_posts]


@router.get("/{slug_or_id}", response_model=BlogPostDetail)
async def get_digest(slug_or_id: str) -> BlogPostDetail:
    """Retrieve full blog post / briefing by ID or slug."""
    is_uuid = False
    parsed_id: uuid.UUID | None = None
    try:
        parsed_id = uuid.UUID(slug_or_id)
        is_uuid = True
    except ValueError:
        is_uuid = False

    row: BlogPost | None = None
    try:
        async with deps.get_session_factory()() as session:
            stmt = (
                select(BlogPost).where((BlogPost.id == parsed_id) | (BlogPost.slug == slug_or_id))
                if is_uuid
                else select(BlogPost).where(BlogPost.slug == slug_or_id)
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
    except Exception as exc:
        logger.warning("digest_get_db_unavailable", error=type(exc).__name__)

    if row is None:
        row = deps.get_scanner().get_memory_post(slug_or_id)

    if row is None:
        raise MetaSearchError(
            ErrorCode.INVALID_REQUEST,
            "Blog post not found",
            status_code=404,
        )

    return _to_detail(row)


@router.post(
    "/scan",
    response_model=list[BlogPostSummary],
    dependencies=[Depends(require_admin)],
)
async def trigger_scan(
    payload: TriggerScanRequest | None = None,
) -> list[BlogPostSummary]:
    """Trigger an on-demand scan to synthesize fresh blog briefings."""
    scanner = deps.get_scanner()
    topic_id = payload.topic if payload else None

    if topic_id:
        if topic_id not in scanner.topics:
            raise MetaSearchError(
                ErrorCode.INVALID_REQUEST,
                f"Unknown topic: {topic_id}. Valid topics: {list(scanner.topics.keys())}",
                status_code=400,
            )
        post = await scanner.scan_topic(topic_id)
        created_posts = [post]
    else:
        created_posts = await scanner.scan_all()

    return [_to_summary(p) for p in created_posts]
