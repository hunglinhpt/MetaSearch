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

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from app.api import deps
from app.db.models import BlogPost
from app.db.session import get_sessionmaker

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
    """List published blog posts / daily briefings."""
    async with get_sessionmaker()() as session:
        stmt = select(BlogPost).order_by(desc(BlogPost.created_at)).offset(offset).limit(limit)
        if topic:
            stmt = stmt.where(BlogPost.topic == topic)

        result = await session.execute(stmt)
        rows = result.scalars().all()

        return [
            BlogPostSummary(
                id=str(r.id),
                slug=r.slug,
                topic=r.topic,
                title=r.title,
                summary=r.summary,
                tags=r.tags,
                published_at=r.published_at.isoformat() if r.published_at else None,
                created_at=r.created_at.isoformat() if r.created_at else None,
            )
            for r in rows
        ]


@router.get("/{slug_or_id}", response_model=BlogPostDetail)
async def get_digest(slug_or_id: str) -> BlogPostDetail:
    """Retrieve full blog post / briefing by ID or slug."""
    async with get_sessionmaker()() as session:
        # Check if valid UUID
        is_uuid = False
        parsed_id: uuid.UUID | None = None
        try:
            parsed_id = uuid.UUID(slug_or_id)
            is_uuid = True
        except ValueError:
            is_uuid = False

        if is_uuid:
            stmt = select(BlogPost).where(BlogPost.id == parsed_id)
        else:
            stmt = select(BlogPost).where(BlogPost.slug == slug_or_id)

        result = await session.execute(stmt)
        row = result.scalar_one_or_none()

        if not row:
            raise HTTPException(status_code=404, detail="Blog post not found")

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


@router.post("/scan", response_model=list[BlogPostSummary])
async def trigger_scan(
    payload: TriggerScanRequest | None = None,
) -> list[BlogPostSummary]:
    """Trigger an on-demand scan to synthesize fresh blog briefings."""
    scanner = deps.get_scanner()
    topic_id = payload.topic if payload else None

    if topic_id:
        if topic_id not in scanner.topics:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown topic: {topic_id}. Valid topics: {list(scanner.topics.keys())}",
            )
        post = await scanner.scan_topic(topic_id)
        created_posts = [post]
    else:
        created_posts = await scanner.scan_all()

    return [
        BlogPostSummary(
            id=str(p.id),
            slug=p.slug,
            topic=p.topic,
            title=p.title,
            summary=p.summary,
            tags=p.tags,
            published_at=p.published_at.isoformat() if p.published_at else None,
            created_at=p.created_at.isoformat() if p.created_at else None,
        )
        for p in created_posts
    ]
