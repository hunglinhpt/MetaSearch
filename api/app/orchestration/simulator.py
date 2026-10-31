"""Dream-RSI Replay Simulator (docs/04 §Agents, Google DeepMind Dream-RSI pattern).

Turns historical searches and scraped documents stored in PostgreSQL/SQLite
into an offline 'World Model' replay simulator. This allows:
1. Zero-cost policy evaluation (testing planner sub-query and rerank strategies).
2. Offline benchmarking without outbound network requests or scraper costs.
3. Fast regression testing of synthesis prompts and citation validators.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models import Document
from app.db.session import get_sessionmaker
from app.extraction.scraper import PageContent, PageFailure
from app.retrieval.canonical import canonicalize
from app.retrieval.models import RawResult, ScoredResult
from app.retrieval.scoring import tokenize

logger = get_logger(__name__)


@dataclass(slots=True)
class SimulationOutcome:
    query: str
    results: list[ScoredResult]
    latency_ms: int
    replayed_from_db: int


class ReplaySimulator:
    """Offline World Model: replays past search & scrape data with zero network calls."""

    def __init__(self, sessionmaker=None) -> None:
        self._sessionmaker = sessionmaker or get_sessionmaker()

    async def search(
        self,
        query: str,
        *,
        k: int = 5,
        categories: list[str] | None = None,
    ) -> SimulationOutcome:
        """Search the accumulated historical documents in the database."""
        started = time.perf_counter()
        q_tokens = set(tokenize(query))

        rows: list[Document] = []
        try:
            async with self._sessionmaker() as session:
                # Fetch recent documents from DB
                stmt = select(Document).order_by(Document.fetched_at.desc()).limit(200)
                rows = list((await session.execute(stmt)).scalars().all())
        except Exception as exc:
            logger.warning("replay_simulator_db_unavailable", error=str(exc))

        scored_items: list[tuple[float, Document]] = []
        for doc in rows:
            text = f"{doc.title or ''} {(doc.content_md or '')[:500]}"
            doc_tokens = set(tokenize(text))
            if not doc_tokens:
                continue
            overlap = len(q_tokens & doc_tokens)
            if overlap > 0:
                relevance = overlap / max(len(q_tokens), 1)
                scored_items.append((relevance, doc))

        scored_items.sort(key=lambda pair: -pair[0])
        top_docs = scored_items[:k]

        results: list[ScoredResult] = []
        for rank, (score, doc) in enumerate(top_docs, start=1):
            domain = (urlsplit(doc.canonical_url).hostname or "").lower()
            snippet = (doc.content_md or "")[:250].replace("\n", " ").strip()
            results.append(
                ScoredResult(
                    url=doc.canonical_url,
                    canonical_url=canonicalize(doc.canonical_url),
                    title=doc.title or doc.canonical_url,
                    snippet=snippet,
                    domain=domain,
                    score=round(score, 4),
                    consensus=1,
                    best_rank=rank,
                    adapters=["replay_simulator"],
                    engines=["replay_db"],
                    published_at=doc.published_at,
                )
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        return SimulationOutcome(
            query=query,
            results=results,
            latency_ms=latency_ms,
            replayed_from_db=len(results),
        )

    async def scrape(self, url: str) -> PageContent | PageFailure:
        """Instant offline scrape replay from DB-cached documents."""
        canon = canonicalize(url)
        doc = None
        try:
            async with self._sessionmaker() as session:
                stmt = select(Document).where(Document.canonical_url == canon)
                doc = (await session.execute(stmt)).scalar_one_or_none()
        except Exception as exc:
            logger.warning("replay_simulator_db_unavailable", error=str(exc))

        if doc and doc.content_md:
            return PageContent(
                url=url,
                final_url=doc.canonical_url,
                title=doc.title or "",
                markdown=doc.content_md,
                extractor_used=f"replay:{doc.extractor_used or 'cache'}",
                fetched_at=doc.fetched_at or datetime.now(UTC),
            )
        return PageFailure(url=url, status="failed", error="URL not found in replay simulator pool")

    async def seed_from_results(self, raws: list[RawResult]) -> int:
        """Utility to check how many raw results are available in offline replay."""
        count = 0
        try:
            async with self._sessionmaker() as session:
                for r in raws:
                    canon = canonicalize(r.url)
                    stmt = select(Document.id).where(Document.canonical_url == canon)
                    if (await session.execute(stmt)).scalar_one_or_none():
                        count += 1
        except Exception as exc:
            logger.warning("replay_simulator_db_unavailable", error=str(exc))
        return count
