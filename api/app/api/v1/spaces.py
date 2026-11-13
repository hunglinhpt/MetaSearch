"""Spaces CRUD + crawl (docs/20 P1-6 — Perplexity-Spaces-lite, v2.6).

GET    /v1/spaces                  -> list the current key's spaces
POST   /v1/spaces                  -> create (slug auto-derived, unique per key, capped)
GET    /v1/spaces/{slug}           -> one space
PATCH  /v1/spaces/{slug}           -> update fields
DELETE /v1/spaces/{slug}           -> delete (404 unless owner)
POST   /v1/spaces/{slug}/crawl     -> map domain roots + scrape pinned URLs into the index

Search inside a space: pass ``space=<slug>`` to /v1/search or /v1/answer.
Tenancy by ``current_api_key_id``; DB down -> list empty storage="memory",
writes refused 503 (same contract as /v1/prefs/boosts).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import APIRouter
from sqlalchemy import Table, delete, select

from app.api import deps
from app.api.v1.schemas import (
    SpaceCreateRequest,
    SpaceListResponse,
    SpaceOut,
    SpaceUpdateRequest,
)
from app.config import get_settings
from app.core.auth import current_api_key_id
from app.core.errors import ErrorCode, MetaSearchError
from app.core.logging import get_logger
from app.db.models import Space
from app.db.session import get_engine
from app.extraction.sitemap import map_site
from app.retrieval.spaces import invalidate, slugify

router = APIRouter(tags=["spaces"])
logger = get_logger(__name__)

_table_ready = False


async def _ensure_table() -> bool:
    global _table_ready
    if _table_ready:
        return True
    try:
        table = cast(Table, Space.__table__)
        async with get_engine().begin() as conn:
            await conn.run_sync(table.create, checkfirst=True)
        _table_ready = True
        return True
    except Exception as exc:
        logger.warning("spaces_table_ensure_failed", error=type(exc).__name__)
        return False


def _owner() -> str:
    return current_api_key_id.get() or "anonymous"


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _out(row: Space) -> SpaceOut:
    return SpaceOut(
        id=str(row.id),
        name=row.name,
        slug=row.slug,
        description=row.description or "",
        domains=list(row.domains or []),
        pinned_urls=list(row.pinned_urls or []),
        query_terms=list(row.query_terms or []),
        restrict=bool(row.restrict),
        last_crawl_at=_iso(row.last_crawl_at),
        created_at=_iso(row.created_at),
    )


async def _get_owned(session, slug: str) -> Space | None:
    return (
        await session.execute(select(Space).where(Space.api_key_id == _owner(), Space.slug == slug))
    ).scalar_one_or_none()


@router.get("/spaces", response_model=SpaceListResponse)
async def list_spaces() -> SpaceListResponse:
    if not await _ensure_table():
        return SpaceListResponse(items=[], storage="memory")
    try:
        async with deps.get_session_factory()() as session:
            rows = (
                (
                    await session.execute(
                        select(Space)
                        .where(Space.api_key_id == _owner())
                        .order_by(Space.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )
        return SpaceListResponse(items=[_out(r) for r in rows], storage="db")
    except Exception as exc:
        logger.warning("spaces_list_failed", error=type(exc).__name__)
        return SpaceListResponse(items=[], storage="memory")


@router.post("/spaces", response_model=SpaceOut, status_code=201)
async def create_space(request: SpaceCreateRequest) -> SpaceOut:
    settings = get_settings()
    if not settings.spaces_enabled:
        raise MetaSearchError(ErrorCode.INVALID_REQUEST, "spaces are disabled", status_code=400)
    if not await _ensure_table():
        raise MetaSearchError(ErrorCode.INTERNAL, "spaces storage unavailable", status_code=503)
    async with deps.get_session_factory()() as session:
        count = (await session.execute(select(Space.id).where(Space.api_key_id == _owner()))).all()
        if len(count) >= settings.spaces_max_per_key:
            raise MetaSearchError(
                ErrorCode.INTERNAL,
                "space cap reached for this key (delete spaces or raise SPACES_MAX_PER_KEY)",
                status_code=507,
            )
        existing = {
            r[0]
            for r in (
                await session.execute(select(Space.slug).where(Space.api_key_id == _owner()))
            ).all()
        }
        base = slugify(request.name)
        slug = base
        i = 2
        while slug in existing:
            slug = f"{base}-{i}"
            i += 1
        row = Space(
            id=uuid.uuid4(),
            api_key_id=_owner(),
            name=request.name,
            slug=slug,
            description=request.description or None,
            domains=[d.strip().lower() for d in request.domains if d.strip()],
            pinned_urls=[u.strip() for u in request.pinned_urls if u.strip()],
            query_terms=[t.strip() for t in request.query_terms if t.strip()],
            restrict=request.restrict,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    invalidate(_owner())
    return _out(row)


@router.get("/spaces/{slug}", response_model=SpaceOut)
async def get_space(slug: str) -> SpaceOut:
    if not await _ensure_table():
        raise MetaSearchError(ErrorCode.INTERNAL, "spaces storage unavailable", status_code=503)
    async with deps.get_session_factory()() as session:
        row = await _get_owned(session, slug)
    if row is None:
        raise MetaSearchError(ErrorCode.NOT_FOUND, "space not found", status_code=404)
    return _out(row)


@router.patch("/spaces/{slug}", response_model=SpaceOut)
async def update_space(slug: str, request: SpaceUpdateRequest) -> SpaceOut:
    if not await _ensure_table():
        raise MetaSearchError(ErrorCode.INTERNAL, "spaces storage unavailable", status_code=503)
    async with deps.get_session_factory()() as session:
        row = await _get_owned(session, slug)
        if row is None:
            raise MetaSearchError(ErrorCode.NOT_FOUND, "space not found", status_code=404)
        data = request.model_dump(exclude_unset=True)
        for key, value in data.items():
            if key in ("domains", "pinned_urls", "query_terms") and value is not None:
                value = [str(v).strip() for v in value if str(v).strip()]
            setattr(row, key, value)
        await session.commit()
        await session.refresh(row)
    invalidate(_owner())
    return _out(row)


@router.delete("/spaces/{slug}")
async def delete_space(slug: str) -> dict[str, Any]:
    if not await _ensure_table():
        raise MetaSearchError(ErrorCode.INTERNAL, "spaces storage unavailable", status_code=503)
    async with deps.get_session_factory()() as session:
        row = await _get_owned(session, slug)
        if row is None:
            raise MetaSearchError(ErrorCode.NOT_FOUND, "space not found", status_code=404)
        await session.execute(delete(Space).where(Space.id == row.id))
        await session.commit()
    invalidate(_owner())
    return {"deleted": True, "slug": slug}


@router.post("/spaces/{slug}/crawl")
async def crawl_space(slug: str, max_pages: int = 20) -> dict[str, Any]:
    """Map each domain root + scrape pinned URLs, persist into the index."""
    settings = get_settings()
    if not settings.spaces_enabled:
        raise MetaSearchError(ErrorCode.INVALID_REQUEST, "spaces are disabled", status_code=400)
    if not await _ensure_table():
        raise MetaSearchError(ErrorCode.INTERNAL, "spaces storage unavailable", status_code=503)
    async with deps.get_session_factory()() as session:
        row = await _get_owned(session, slug)
    if row is None:
        raise MetaSearchError(ErrorCode.NOT_FOUND, "space not found", status_code=404)

    from app.api.v1.schemas import CrawlRequest
    from app.api.v1.site import crawl_site  # reuse the BFS crawler (no nested admit)

    scraper = deps.get_scraper()
    targets: list[str] = list(row.pinned_urls or [])
    for domain in row.domains or []:
        root = f"https://{domain}/"
        try:
            mapped = await map_site(
                scraper, root, max_urls=min(50, settings.map_max_urls), timeout_s=8.0
            )
            targets.extend(mapped.urls[:50])
        except Exception as exc:
            logger.info("space_map_domain_failed", domain=domain, error=type(exc).__name__)
    # Dedupe, cap, crawl depth 1 from the space root(s).
    seen: set[str] = set()
    uniq: list[str] = []
    for t in targets:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    cap = min(max_pages, settings.crawl_max_pages)
    results: list[dict[str, Any]] = []
    if uniq:
        resp = await crawl_site(
            scraper,
            CrawlRequest(url=uniq[0], depth=1, max_pages=min(len(uniq), cap), max_chars=100_000),
        )
        results = [p.model_dump() for p in resp.pages]
        # Remaining targets are scraped directly (depth-0).
        rest = uniq[1:cap]
        if rest:
            pages = await scraper.scrape_many(rest, timeout_s=None)
            from app.extraction.chunker import late_chunk_markdown
            from app.extraction.scraper import PageContent
            from app.orchestration import persist

            ok_pages = [p for p in pages if isinstance(p, PageContent)]
            chunks = []
            for page in ok_pages:
                chunks.extend(
                    late_chunk_markdown(
                        page.markdown, document_url=page.url, source_title=page.title or ""
                    )
                )
            if ok_pages:
                persist.persist_pages(ok_pages, chunks)
            for p in pages:
                if isinstance(p, PageContent):
                    results.append({"url": p.url, "status": "ok", "title": p.title or ""})
                else:
                    results.append({"url": p.url, "status": p.status, "error": p.error})

    async with deps.get_session_factory()() as session:
        row2 = await _get_owned(session, slug)
        if row2 is not None:
            row2.last_crawl_at = datetime.now(UTC)
            await session.commit()
    invalidate(_owner())
    return {"space": slug, "targets": len(uniq), "pages": results, "capped": cap}
