"""Site map/crawl endpoints for agents (docs/20 P1-7, v2.6 — Tavily map/crawl-lite).

POST /v1/map   -> discover URLs of a site (sitemap.xml first, same-host HTML
                  links as complement/fallback), SSRF+robots guarded.
POST /v1/crawl -> BFS-crawl a site up to ``depth`` (same-host only) through the
                  existing Scraper (robots/SSRF/pinning/extract chain), persist
                  the pages into the document/chunk index, return bounded markdown.

Both respect the request budget + admission like /v1/scrape.
"""

from __future__ import annotations

import time

from fastapi import APIRouter

from app.api import deps
from app.api.v1.schemas import (
    CrawlPageItem,
    CrawlRequest,
    CrawlResponse,
    MapRequest,
    MapResponse,
)
from app.config import get_settings
from app.core.admission import admit
from app.core.budget import create_budget_for_route, use_budget
from app.extraction.chunker import late_chunk_markdown
from app.extraction.scraper import PageContent, PageFailure
from app.extraction.sitemap import _normalize, extract_links, map_site, same_host

router = APIRouter(tags=["site"])


@router.post("/map", response_model=MapResponse, response_model_exclude_none=True)
async def map_endpoint(request: MapRequest) -> MapResponse:
    budget = create_budget_for_route("map", timeout_ms=request.timeout_ms)
    async with admit("map"):
        with use_budget(budget):
            settings = get_settings()
            result = await map_site(
                deps.get_scraper(),
                request.url,
                max_urls=min(request.max_urls, settings.map_max_urls),
                timeout_s=10.0,
            )
    return MapResponse(
        root=result.root,
        urls=result.urls,
        source=result.source,
        truncated=result.truncated,
    )


async def crawl_site(scraper, request: CrawlRequest) -> CrawlResponse:
    """BFS-crawl without admission/budget wrapping — used by /v1/crawl AND by
    /v1/spaces/{slug}/crawl (which must not nest a second ``admit("crawl")``
    slot: with a tier limit of 1 that would self-deadlock)."""
    started = time.perf_counter()
    settings = get_settings()
    max_pages = min(request.max_pages, settings.crawl_max_pages)
    depth = min(request.depth, settings.crawl_max_depth)
    root = _normalize(request.url)
    visited: set[str] = set()
    frontier: list[str] = [root] if root else []
    items: list[CrawlPageItem] = []
    ok_pages: list[PageContent] = []

    for _level in range(depth):
        if not frontier or len(visited) >= max_pages:
            break
        batch = frontier[: max_pages - len(visited)]
        pages = await scraper.scrape_many(batch, timeout_s=None)
        next_frontier: list[str] = []
        for page in pages:
            if isinstance(page, PageFailure):
                visited.add(_normalize(page.url) or page.url)
                items.append(CrawlPageItem(url=page.url, status=page.status, error=page.error))
                continue
            norm = _normalize(page.final_url or page.url) or page.url
            visited.add(norm)
            md = page.markdown
            truncated = False
            if len(md) > request.max_chars:
                md = md[: request.max_chars]
                truncated = True
            items.append(
                CrawlPageItem(
                    url=page.url,
                    status="ok",
                    title=page.title or "",
                    markdown=md,
                    truncated=truncated,
                )
            )
            ok_pages.append(page)
            # BFS: same-host links found in the extracted markdown.
            for link in extract_links(page.markdown, page.final_url or page.url, markdown=True):
                if link not in visited and link not in next_frontier and same_host(link, root):
                    next_frontier.append(link)
        frontier = next_frontier

    indexed = False
    if ok_pages:
        from app.orchestration import persist

        chunks = []
        for page in ok_pages:
            # docs/20 P1-3: long pages are segmented before chunking so chunk
            # boundaries respect the late-chunking embed windows.
            chunks.extend(
                late_chunk_markdown(
                    page.markdown, document_url=page.url, source_title=page.title or ""
                )
            )
        persist.persist_pages(ok_pages, chunks)
        indexed = True

    return CrawlResponse(
        root=root or request.url,
        visited=len(visited),
        pages=items,
        indexed=indexed,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


@router.post("/crawl", response_model=CrawlResponse, response_model_exclude_none=True)
async def crawl_endpoint(request: CrawlRequest) -> CrawlResponse:
    budget = create_budget_for_route("crawl", timeout_ms=request.timeout_ms)
    async with admit("crawl"):
        with use_budget(budget):
            return await crawl_site(deps.get_scraper(), request)
