"""One real call per keyless engine on this machine, sequential, low volume.

Answers: do the adapters work from this machine (TLS interception, bot blocks)?
Run from api/: python scripts/live_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

os.environ.update(
    ENVIRONMENT="dev",
    LOG_LEVEL="ERROR",
    REDIS_URL="redis://127.0.0.1:6399/0",
    ENGINE_STORE_BACKEND="memory",
    CACHE_ENABLED="false",
    SEARXNG_ENABLED="false",
)
sys.path.insert(0, os.getcwd())

from app.config import Settings
from app.retrieval.registry import build_adapters

QUERIES = {
    "ddg": "cổ phiếu HQC tin tức",
    "arxiv": "retrieval augmented generation",
    "wikipedia": "Thị trường chứng khoán Việt Nam",
    "stackoverflow": "fastapi streaming sse",
    "reddit": "vietnam stock market",
    "github": "searxng",
    "pubmed": "metformin",
    "huggingface": "bge-m3",
    "semanticscholar": "retrieval augmented generation",
    "yahoo_finance": "AAPL stock",
    "sec_edgar": "AAPL 10-K",
    "stooq": "gold price",
    "google_news": "HQC cổ phiếu",
    "fengshui": "tử vi tuổi Giáp Tý",
    "viet_stock": "cổ phiếu HQC báo cáo tài chính",
}


async def main() -> None:
    adapters = build_adapters(Settings(_env_file=None, searxng_enabled=False))
    ok = 0
    for engine, query in QUERIES.items():
        adapter = adapters.get(engine)
        t0 = time.perf_counter()
        try:
            res = await asyncio.wait_for(adapter.search(query, max_results=3, timeout_s=8.0), 12)
            ms = (time.perf_counter() - t0) * 1000
            top = res[0] if res else None
            pub = getattr(top, "published_at", None) if top else None
            ok += bool(res)
            print(
                f"LIVE {engine:<16} ok     n={len(res):<2} {ms:6.0f}ms pub={pub} "
                f"{(top.url if top else '')[:70]}"
            )
        except Exception as exc:
            ms = (time.perf_counter() - t0) * 1000
            print(f"LIVE {engine:<16} FAILED {ms:6.0f}ms {type(exc).__name__}: {str(exc)[:110]}")
    print(f"LIVE summary: {ok}/{len(QUERIES)} returned results")

    # Scrape tier from this machine: TLS trust store + pinning on a real page.
    from app.extraction.scraper import PageContent, Scraper

    scraper = Scraper(
        Settings(_env_file=None, jina_keyless=False, crawl4ai_url="http://127.0.0.1:1")
    )
    for url in (
        "https://vi.wikipedia.org/wiki/Th%E1%BB%8B_tr%C6%B0%E1%BB%9Dng_ch%E1%BB%A9ng_kho%C3%A1n",
        "https://cafef.vn/",
        "https://vnexpress.net/kinh-doanh",
    ):
        t0 = time.perf_counter()
        page = await scraper.scrape(url, timeout_s=8.0)
        ms = (time.perf_counter() - t0) * 1000
        if isinstance(page, PageContent):
            size = len(page.markdown)
            print(f"SCRAPE ok     {ms:6.0f}ms {size:>7} chars {page.extractor_used} {url[:50]}")
        else:
            print(f"SCRAPE {page.status:<6} {ms:6.0f}ms {page.error[:80]} {url[:50]}")
    from app.http import dispose_http_client

    await dispose_http_client()


asyncio.run(main())
