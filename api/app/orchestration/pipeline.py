"""Search + answer pipelines (docs/03-search-pipeline.md, 11 stages).

The answer pipeline is a single async generator of ``(event, data)`` tuples so
/v1/answer can serve both JSON (consume + reduce) and SSE (forward) from the
same code path. One slow engine never fails the request: partial results win.

Phase 2 adds: L1/L2 cache lookups, per-engine health recording, the rerank
stage and cached-answer replay.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import re
import time
import unicodedata
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select

from app.cache import (
    CacheClient,
    answer_cache_key,
    deserialize_results,
    search_cache_key,
    search_ttl_s,
    serialize_results,
)
from app.config import Settings
from app.core.budget import get_current_budget
from app.core.errors import ErrorCode, MetaSearchError
from app.core.logging import get_logger
from app.core.metrics import CACHE_EVENTS, ENGINE_CALLS, ENGINE_LATENCY, STAGE_DURATION
from app.core.offload import run_cpu
from app.core.singleflight import SingleFlight
from app.db.base import EMBEDDING_DIM
from app.db.models import Answer, SemanticCache
from app.db.session import get_sessionmaker
from app.extraction.chunker import Chunk, chunk_markdown
from app.extraction.scraper import PageContent, PageFailure, Scraper
from app.llm.citations import validate_citations
from app.llm.embeddings import EmbeddingClient, EmbeddingError
from app.llm.gateway import LLMError, LLMGateway, Usage, record_usage
from app.llm.prompts import SYSTEM_PROMPT, build_context, build_messages, render_context
from app.llm.providers import LLMOverride
from app.llm.tokens import make_counter
from app.orchestration import persist
from app.retrieval.adapters.base import SearchAdapter
from app.retrieval.canonical import canonicalize
from app.retrieval.merger import diversity_cap, merge
from app.retrieval.models import EngineConfig, EngineStatus, RawResult, ScoredResult
from app.retrieval.reranker import Reranker, RerankerUnavailable, heuristic_order
from app.retrieval.scoring import heuristic_chunk_score
from app.retrieval.vn_tickers import is_vn_ticker

logger = get_logger(__name__)

Event = tuple[str, dict]
OnEvent = Callable[[Event], None] | None

_background_tasks: set[asyncio.Task[None]] = set()

_QUANT_ADAPTERS = frozenset({"viet_stock", "stooq", "yahoo_finance", "sec_edgar", "fengshui"})

_DOLLAR_TICKER_RE = re.compile(r"\$[a-zA-Z]{1,6}\b")


def _has_vn_ticker(query: str) -> bool:
    return any(is_vn_ticker(word) for word in re.findall(r"\b[A-Za-z0-9]{3}\b", query))


def infer_categories_from_query(query: str, *, news: bool = False) -> set[str]:
    """Automatically infer vertical categories from unambiguous domain signals in ``query``."""
    inferred: set[str] = set()
    if news:
        inferred.add("news")
    q = query.lower()
    if len(q) < 3:
        return inferred

    def _has_term(terms) -> bool:
        # Word-boundary match for ASCII terms so short tickers/abbreviations do
        # not substring-match inside other words ("nav" in "navigation",
        # "eps" in "steps"). Boundaries only apply where the term itself starts/
        # ends with an ASCII word char, so "doi:" still matches "doi:10..." and
        # Vietnamese phrases keep plain substring semantics.
        for t in terms:
            pre = r"(?<![a-z0-9])" if t[0].isascii() and t[0].isalnum() else ""
            post = r"(?![a-z0-9])" if t[-1].isascii() and t[-1].isalnum() else ""
            if re.search(pre + re.escape(t) + post, q):
                return True
        return False

    fin_terms = (
        "cổ phiếu",
        "chứng khoán",
        "bctc",
        "báo cáo tài chính",
        "cổ tức",
        "thị giá",
        "vốn hóa",
        "p/e",
        "p/b",
        "eps",
        "roe",
        "roa",
        "ebitda",
        "fibonacci",
        "ichimoku",
        "altman",
        "piotroski",
        "beneish",
        "camels",
        "nim",
        "casa",
        "vn-index",
        "vnindex",
        "hsx",
        "hnx",
        "hose",
        "upcom",
        "trao tay",
        "cổ đông",
        "10-k",
        "10-q",
        "sec edgar",
        "yahoo finance",
        "giá vàng",
        "lợi suất",
        "treasury yield",
        "kế hoạch kinh doanh",
        "kết quả kinh doanh",
        "pháp y tài chính",
    )
    if _has_term(fin_terms) or _DOLLAR_TICKER_RE.search(q) or _has_vn_ticker(query):
        inferred.add("financial")

    fengshui_terms = (
        "phong thủy",
        "bát trạch",
        "cung mệnh",
        "can chi",
        "ngũ hành",
        "sinh khí",
        "tử vi",
        "hướng nhà",
        "tuổi làm ăn",
        "tam hợp",
        "tứ hành xung",
        "mệnh kim",
        "mệnh mộc",
        "mệnh thủy",
        "mệnh hỏa",
        "mệnh thổ",
    )
    if _has_term(fengshui_terms):
        inferred.add("fengshui")

    news_terms: tuple[str, ...] = (
        "tin tức",
        "thời sự",
        "mới nhất",
        "breaking news",
        "bản tin hôm nay",
    )
    if _has_term(news_terms):
        inferred.add("news")

    acad_terms: tuple[str, ...] = (
        "arxiv",
        "pubmed",
        "semanticscholar",
        "clinical trial",
        "nghiên cứu lâm sàng",
        "peer-reviewed",
    )
    if _has_term(acad_terms):
        inferred.add("academic")

    dev_terms = (
        "stackoverflow",
        "stackexchange",
        "stack exchange",
        "github repo",
        "huggingface",
        "traceback",
        "stacktrace",
        "syntaxerror",
        "typeerror",
        "nullpointerexception",
        "hacker news",
        "news.ycombinator",
    )
    if _has_term(dev_terms):
        inferred.add("developer")

    # v2.4.0 global (docs/19): social networks & forums
    social_terms = (
        "reddit",
        "twitter",
        "x.com",
        "tweet",
        "mastodon",
        "bluesky",
        "bsky",
        "linkedin",
        "facebook",
        "instagram",
        "tiktok",
        "youtube",
        "telegram",
        "discord",
        "forum",
        "diễn đàn",
        "thảo luận",
        "Ý kiến cộng đồng",
        "hacker news",
        "news.ycombinator",
    )
    if _has_term(social_terms):
        inferred.add("social")

    # v2.4.0 global: scholarly sources beyond arxiv/pubmed
    acad_terms += (
        "doi:",
        "doi.org",
        "openalex",
        "crossref",
        "europe pmc",
        "europepmc",
        "ssrn",
        "dblp",
        "semantic scholar",
        "google scholar",
        "journal",
        "tạp chí",
        "preprint",
        "peer review",
        "meta-analysis",
        "systematic review",
        "research paper",
        "bài báo khoa học",
    )
    if _has_term(acad_terms):
        inferred.add("academic")

    # v2.4.0 global: fund/institutional research signals
    global_fin_terms = (
        "etf",
        "nav",
        "13f",
        "prospectus",
        "fact sheet",
        "morningstar",
        "seeking alpha",
        "reuters",
        "bloomberg",
        "imf",
        "world bank",
        "oecd",
        "bis",
        "ecb",
        "federal reserve",
        "the fed",
        "fred",
        "13f filing",
        "annual report",
        "báo cáo thường niên",
        "quỹ đầu tư",
        "mutual fund",
        "hedge fund",
        "pension fund",
        "sovereign wealth",
    )
    if _has_term(global_fin_terms):
        inferred.add("financial")

    # v2.4.0 global: world news signals (English included)
    news_terms += ("gdelt", "world news", "global news", "international news", "breaking")
    if _has_term(news_terms):
        inferred.add("news")

    return inferred


@dataclass(slots=True)
class SearchParams:
    query: str
    lang: str | None = None
    k: int = 10
    engines: list[str] | None = None
    categories: list[str] = field(default_factory=lambda: ["general"])
    freshness_days: int | None = None
    include_snippets: bool = True
    news: bool = False
    timeout_ms: int | None = None
    # Applied after the search (and its cache), so filtered and unfiltered calls share one
    # fan-out. A filter that removes every result marks the outcome incomplete, not empty.
    include_domains: list[str] | None = None
    exclude_domains: list[str] | None = None
    # docs/19 W4: deep=True → fan out to ALL enabled engines, ignoring the
    # category filter, so "deepest possible" search is one flag away.
    deep: bool = False


@contextlib.contextmanager
def _stage(name: str):
    """Observe how long one pipeline stage took, also when it raises."""
    started = time.perf_counter()
    try:
        yield
    finally:
        STAGE_DURATION.labels(stage=name).observe(time.perf_counter() - started)


def _norm_domain(domain: str) -> str:
    d = domain.strip().lower().rstrip(".")
    return d[4:] if d.startswith("www.") else d


def _has_domain_filter(params: SearchParams) -> bool:
    return bool(
        getattr(params, "include_domains", None) or getattr(params, "exclude_domains", None)
    )


def _domain_in(host: str, wanted: list[str]) -> bool:
    h = _norm_domain(host)
    return any(h == w or h.endswith("." + w) for w in wanted)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _age_s(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - value).total_seconds()))


_INCOMPLETE_ENGINE_STATUSES = frozenset(
    {"error", "timeout", "circuit_broken", "local_overload", "skipped_breaker", "cancelled"}
)


def _has_incomplete_status(statuses: list[EngineStatus]) -> bool:
    return any(s.status in _INCOMPLETE_ENGINE_STATUSES for s in statuses)


_OPTIONAL_CONTEXT_PARTS = frozenset(
    {"chunks", "sources", "context_md", "messages", "system_prompt", "domain_intelligence"}
)
_DEFAULT_CONTEXT_INCLUDE = ("chunks", "sources")
_EMPTY_MARKS: dict[str, object] = {
    "source_url": None,
    "data_kind": None,
    "fetched_at": None,
    "published_at": None,
    "as_of": None,
    "age_s": None,
}


def _data_marks(r: ScoredResult) -> dict[str, object]:
    """Provenance and time marks of one result. Unknown stays None, never invented."""
    return {
        "source_url": r.source_url,
        "data_kind": r.data_kind,
        "fetched_at": None,
        "published_at": _iso(r.published_at),
        "as_of": _iso(r.as_of),
        # Same fallback as /v1/search: news items carry published_at, not as_of.
        "age_s": _age_s(r.as_of or r.published_at),
    }


def _apply_domain_filter(outcome: SearchOutcome, params: SearchParams) -> SearchOutcome:
    if not _has_domain_filter(params):
        return outcome
    include = [_norm_domain(d) for d in params.include_domains or [] if d.strip()]
    exclude = [_norm_domain(d) for d in params.exclude_domains or [] if d.strip()]
    kept = []
    for r in outcome.results:
        host = r.domain or (urlsplit(r.url).hostname or "")
        if include and not _domain_in(host, include):
            continue
        if exclude and _domain_in(host, exclude):
            continue
        kept.append(r)
    # Results existed but the filter removed them all: say so, never claim a true empty.
    complete = outcome.complete and (bool(kept) or not outcome.results)
    return dataclasses.replace(outcome, results=kept, complete=complete)


@dataclass(slots=True)
class SearchOutcome:
    query_id: uuid.UUID
    results: list[ScoredResult]
    statuses: list[EngineStatus]
    latency_ms: int
    cached: bool = False
    complete: bool = True
    age_s: int | None = None


def normalize_query(raw: str) -> str:
    """NFC normalize, trim, collapse inner whitespace."""
    normalized = unicodedata.normalize("NFC", raw)
    return " ".join(normalized.split())


class SearchPipeline:
    """Cache check -> fan-out -> merge -> score -> diversity cap (stages 3-7)."""

    def __init__(
        self,
        settings: Settings,
        registry,
        cache: CacheClient | None = None,
        health_registry: Any | None = None,
        health_writer: Any | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.cache = cache
        from app.retrieval.health import EngineBreakerRegistry

        self.health_registry = health_registry or EngineBreakerRegistry(
            threshold=settings.engine_auto_disable_threshold
        )
        self.health_writer = health_writer
        self._single_flight = SingleFlight()

    async def run(self, params: SearchParams, on_event: OnEvent = None) -> SearchOutcome:
        with _stage("search"):
            return await self._run_filtered(params, on_event)

    async def _run_filtered(self, params: SearchParams, on_event: OnEvent = None) -> SearchOutcome:
        if on_event is None:
            normalized = normalize_query(params.query)
            cache_key = search_cache_key(normalized, params, news=params.news)
            outcome = await self._single_flight.run(
                cache_key, lambda: self._run_search(params, on_event=None)
            )
        else:
            outcome = await self._run_search(params, on_event=on_event)
        return _apply_domain_filter(outcome, params)

    async def _run_search(self, params: SearchParams, on_event: OnEvent = None) -> SearchOutcome:
        started = time.perf_counter()
        query_id = uuid.uuid4()
        query = normalize_query(params.query)

        cached_payload = await self._cache_lookup(query, params)
        if cached_payload is not None:
            CACHE_EVENTS.labels(kind="search", outcome="hit").inc()
            cached_at = cached_payload.get("cached_at")
            age_s = max(0, int(time.time() - cached_at)) if cached_at is not None else 0
            return SearchOutcome(
                query_id=uuid.UUID(cached_payload["query_id"]),
                results=deserialize_results(cached_payload["results"]),
                statuses=[
                    EngineStatus(
                        engine=s["engine"], status=s["status"], results=s.get("results", 0)
                    )
                    for s in cached_payload["statuses"]
                ],
                latency_ms=int((time.perf_counter() - started) * 1000),
                cached=True,
                complete=bool(cached_payload.get("complete", True)),
                age_s=age_s,
            )
        CACHE_EVENTS.labels(kind="search", outcome="miss").inc()

        raws, statuses = await self._fanout(query, params, on_event)
        configs = await self.registry.configs()
        results = merge(raws, query=query, registry=configs, news=params.news)
        results = diversity_cap(results, max_per_domain=2, limit=params.k)

        latency_ms = int((time.perf_counter() - started) * 1000)
        if results and self.cache is not None:
            await self._cache_store(query, params, query_id, results, statuses)
        persist.persist_search(
            query_id=query_id,
            raw_text=params.query,
            normalized_text=query,
            lang=params.lang,
            params={
                "k": params.k,
                "categories": params.categories,
                "freshness_days": params.freshness_days,
                "engines": params.engines,
            },
            results=results,
        )
        is_complete = not _has_incomplete_status(statuses)
        return SearchOutcome(
            query_id=query_id,
            results=results,
            statuses=statuses,
            latency_ms=latency_ms,
            complete=is_complete,
        )

    async def _cache_lookup(self, query: str, params: SearchParams) -> dict | None:
        if self.cache is None:
            return None
        payload = await self.cache.get_json(search_cache_key(query, params, news=params.news))
        if not payload or not payload.get("results"):
            return None
        return payload

    async def _cache_store(
        self,
        query: str,
        params: SearchParams,
        query_id: uuid.UUID,
        results: list[ScoredResult],
        statuses: list[EngineStatus],
    ) -> None:
        if self.cache is None:
            return
        ttl_s = search_ttl_s(self.settings, news=params.news)
        if _has_incomplete_status(statuses):
            ttl_s = min(ttl_s, self.settings.cache_partial_ttl_s)
        categories = set(params.categories or [])
        if categories == {"general"} or params.news:
            categories |= infer_categories_from_query(query, news=params.news)
        if "financial" in categories:
            ttl_s = min(ttl_s, 300)
        is_complete = not _has_incomplete_status(statuses)
        await self.cache.set_json(
            search_cache_key(query, params, news=params.news),
            {
                "query_id": str(query_id),
                "cached_at": time.time(),
                "complete": is_complete,
                "results": serialize_results(results),
                "statuses": [
                    {"engine": s.engine, "status": s.status, "results": s.results} for s in statuses
                ],
            },
            ttl_s=ttl_s,
        )

    async def _fanout(
        self, query: str, params: SearchParams, on_event: OnEvent
    ) -> tuple[list[RawResult], list[EngineStatus]]:
        candidates = await self.registry.enabled()
        if params.engines:
            # Explicit engine list wins over category defaults.
            wanted = set(params.engines)
            candidates = [(cfg, ad) for cfg, ad in candidates if cfg.id in wanted]
        elif params.deep:
            # Deep mode (docs/19 W4): every enabled engine joins the fan-out —
            # general + all verticals (social, academic, developer, financial…).
            pass
        else:
            # Vertical engines (arXiv, Wikipedia, VietStock, Stooq, ...) join when
            # the request asks for their category OR when the query unambiguously
            # targets their domain while categories is left at default ["general"].
            categories = set(params.categories or [])
            if categories == {"general"} or params.news:
                categories |= infer_categories_from_query(query, news=params.news)
            candidates = [
                (cfg, ad)
                for cfg, ad in candidates
                if cfg.category == "general" or cfg.category in categories
            ]

        if not candidates:
            raise MetaSearchError(
                ErrorCode.NO_RESULTS,
                "No search engine is enabled for this request",
                details={"requested_engines": params.engines or []},
            )

        candidate_map = {cfg.id: (cfg, ad) for cfg, ad in candidates}
        candidate_ids = [cfg.id for cfg, _ in candidates]
        allowed_ids, skipped_ids = self.health_registry.filter_candidates(candidate_ids)

        raws: list[RawResult] = []
        statuses: list[EngineStatus] = []

        for sid in skipped_ids:
            statuses.append(EngineStatus(engine=sid, status="skipped_breaker", results=0, ms=0))

        active_candidates = [candidate_map[eid] for eid in allowed_ids]

        async def call_engine(cfg: EngineConfig, adapter: SearchAdapter) -> list[RawResult]:
            started = time.perf_counter()
            try:
                results = await adapter.search(
                    query,
                    lang=params.lang,
                    categories=params.categories,
                    freshness_days=params.freshness_days,
                    max_results=cfg.max_results,
                    timeout_s=cfg.timeout_ms / 1000,
                )
            except Exception as exc:
                ms = int((time.perf_counter() - started) * 1000)
                statuses.append(
                    EngineStatus(engine=cfg.id, status="error", ms=ms, error=str(exc)[:200])
                )
                ENGINE_CALLS.labels(engine=cfg.id, outcome="error").inc()
                logger.info("engine_failed", engine=cfg.id, error=str(exc)[:200])
                from app.retrieval.adapters.base import AdapterError
                from app.retrieval.health import Outcome

                outcome = Outcome.UPSTREAM_ERROR
                retry_after_s = None
                if isinstance(exc, AdapterError):
                    outcome = exc.kind
                    retry_after_s = exc.retry_after_s
                elif isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                    outcome = Outcome.TIMEOUT

                self.health_registry.record(cfg.id, outcome, retry_after_s=retry_after_s)
                await self.registry.store.record_result(cfg.id, ok=False, latency_ms=ms)
                if self.health_writer:
                    self.health_writer.record(cfg.id, ok=False, latency_ms=ms)
                if on_event:
                    on_event(("engine_done", {"engine": cfg.id, "results": 0, "ms": ms}))
                return []
            ms = int((time.perf_counter() - started) * 1000)
            statuses.append(EngineStatus(engine=cfg.id, status="ok", results=len(results), ms=ms))
            ENGINE_CALLS.labels(engine=cfg.id, outcome="ok").inc()
            ENGINE_LATENCY.labels(engine=cfg.id).observe(ms / 1000)

            from app.retrieval.health import Outcome

            outcome = Outcome.EMPTY if len(results) == 0 else Outcome.OK
            self.health_registry.record(cfg.id, outcome)
            await self.registry.store.record_result(cfg.id, ok=True, latency_ms=ms)
            if self.health_writer:
                self.health_writer.record(cfg.id, ok=True, latency_ms=ms)

            if on_event:
                on_event(("engine_done", {"engine": cfg.id, "results": len(results), "ms": ms}))
            return results

        tasks = {
            asyncio.create_task(call_engine(cfg, adapter)): cfg.id
            for cfg, adapter in active_candidates
        }
        from app.core.budget import get_current_budget

        budget = get_current_budget()
        hard_timeout = (
            budget.stage("search", self.settings.search_fanout_budget_s)
            if budget
            else self.settings.search_fanout_budget_s
        )
        soft_timeout = min(self.settings.search_soft_budget_s, hard_timeout)
        min_engines = self.settings.search_quorum_min_engines

        done: set[asyncio.Task[Any]] = set()
        pending = set(tasks.keys())
        successful_engines = 0
        start_mono = time.monotonic()

        quorum_met = False
        while pending:
            elapsed = time.monotonic() - start_mono
            remaining_hard = hard_timeout - elapsed
            if remaining_hard <= 0.0:
                break
            # docs/19 W4: deep mode disables the quorum early-return — the whole
            # point is to wait for ALL engines (still bounded by hard budget).
            if not params.deep and elapsed >= soft_timeout and successful_engines >= min_engines:
                quorum_met = True
                break

            step_timeout = min(
                remaining_hard,
                max(0.01, soft_timeout - elapsed) if successful_engines < min_engines else 0.05,
            )
            step_done, pending = await asyncio.wait(
                pending, timeout=step_timeout, return_when=asyncio.FIRST_COMPLETED
            )
            for t in step_done:
                done.add(t)
                with contextlib.suppress(Exception):
                    res = t.result()
                    if res and len(res) > 0:
                        successful_engines += 1

        if pending:
            from app.retrieval.health import Outcome

            outcome = Outcome.CANCELLED if quorum_met else Outcome.TIMEOUT
            status_str = "cancelled" if quorum_met else "timeout"

            for task in pending:
                engine_id = tasks[task]
                statuses.append(EngineStatus(engine=engine_id, status=status_str))
                ENGINE_CALLS.labels(engine=engine_id, outcome=status_str).inc()
                self.health_registry.record(engine_id, outcome)
                if self.health_writer and not quorum_met:
                    self.health_writer.record(engine_id, ok=False, latency_ms=0)
                task.cancel()
            # Let cancellations settle so no task leaks past the request.
            await asyncio.gather(*pending, return_exceptions=True)

        for task in done:
            exc = task.exception()
            if exc is None:
                raws.extend(task.result())
        return raws, statuses


def _score_chunks_sync(
    query: str, results: list[ScoredResult], pages: list[PageContent]
) -> list[tuple[float, Chunk]]:
    scored_chunks: list[tuple[float, Chunk]] = []
    res_by_url: dict[str, ScoredResult] = {}
    for r in results:
        res_by_url[r.url] = r
        res_by_url.setdefault(r.canonical_url, r)

    scraped_urls: set[str] = set()
    for page in pages:
        scraped_urls.add(page.url)
        scraped_urls.add(page.final_url)
        try:
            canon_page_url = canonicalize(page.url)
            scraped_urls.add(canon_page_url)
        except Exception:
            canon_page_url = None
        matched_res = res_by_url.get(page.url) or (
            res_by_url.get(canon_page_url) if canon_page_url else None
        )
        result_score = matched_res.score if matched_res else 0.0
        md_body = page.markdown
        # Preserve quantitative synthesis snippets from vertical adapters even when scraped
        if (
            matched_res is not None
            and matched_res.snippet
            and any(a in _QUANT_ADAPTERS for a in matched_res.adapters)
            and matched_res.snippet[:80] not in md_body
        ):
            md_body = f"{matched_res.snippet}\n\n{md_body}"
        page_chunks = chunk_markdown(md_body, document_url=page.url, source_title=page.title)
        for chunk in page_chunks:
            score = heuristic_chunk_score(query, chunk.content, result_score, chunk.position)
            scored_chunks.append((score, chunk))

    # Fallback: for any search result whose URL could not be scraped (or was blocked),
    # ground the synthesizer using its search snippet so quantitative data is never lost.
    for r in results:
        if r.url in scraped_urls or r.canonical_url in scraped_urls:
            continue
        snippet_text = (r.snippet or "").strip()
        if not snippet_text:
            continue
        fallback_md = f"# {r.title}\n\n{snippet_text}" if r.title else snippet_text
        fallback_chunks = chunk_markdown(
            fallback_md, document_url=r.url, source_title=r.title or r.url
        )
        for chunk in fallback_chunks:
            score = heuristic_chunk_score(query, chunk.content, r.score, chunk.position)
            scored_chunks.append((score, chunk))

    scored_chunks.sort(key=lambda pair: -pair[0])
    return scored_chunks


_FLIGHT_DONE = object()
# A caller that joined a running answer gives up when that answer goes quiet this long.
# The first caller keeps the old behaviour: the LLM gateway's own timeouts bound it.
_FLIGHT_IDLE_TIMEOUT_S = 60.0


class _AnswerFlight:
    """One running answer shared by identical concurrent requests.

    Every subscriber first gets the events produced so far, then the live ones, so the
    first caller and the callers that join later all stream tokens as they come.
    """

    def __init__(self) -> None:
        self.task: asyncio.Task[None] | None = None
        self.events: list[Event] = []
        self.subscribers: list[asyncio.Queue[object]] = []
        self.listeners: list[Callable[[Event], None]] = []

    def subscribe(self, on_event: OnEvent) -> asyncio.Queue[object]:
        queue: asyncio.Queue[object] = asyncio.Queue()
        for event in self.events:
            queue.put_nowait(event)
        self.subscribers.append(queue)
        if on_event is not None:
            self.listeners.append(on_event)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[object], on_event: OnEvent) -> None:
        with contextlib.suppress(ValueError):
            self.subscribers.remove(queue)
        if on_event is not None:
            with contextlib.suppress(ValueError):
                self.listeners.remove(on_event)

    def publish(self, item: object) -> None:
        for queue in self.subscribers:
            queue.put_nowait(item)

    def fan_out(self, event: Event) -> None:
        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception as exc:  # one caller's callback must not break the others
                logger.warning("answer_flight_listener_failed", error=type(exc).__name__)


def _build_answer_flight_key(
    params: SearchParams,
    *,
    model: str | None = None,
    llm_override: LLMOverride | None = None,
    council_mode: bool = False,
    council_endpoints: list[LLMOverride] | None = None,
    domain: str = "auto",
) -> str:
    key_dict = {
        "query": normalize_query(params.query),
        "lang": params.lang,
        "k": params.k,
        "categories": sorted(params.categories or []),
        "engines": sorted(params.engines or []) if params.engines else None,
        "freshness_days": params.freshness_days,
        "include_snippets": params.include_snippets,
        "news": params.news,
        "timeout_ms": params.timeout_ms,
        "include_domains": sorted(params.include_domains or []),
        "exclude_domains": sorted(params.exclude_domains or []),
        "model": model,
        "domain": domain or "auto",
        "council_mode": council_mode,
        "council_endpoints": [
            ep.model_dump() if hasattr(ep, "model_dump") else str(ep)
            for ep in (council_endpoints or [])
        ],
        "llm_override": (
            llm_override.model_dump() if hasattr(llm_override, "model_dump") else str(llm_override)
        )
        if llm_override
        else None,
    }
    raw = json.dumps(key_dict, sort_keys=True, default=str)
    return "an_flight:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class AnswerPipeline:
    """Cache -> search -> scrape -> chunk -> rerank -> synthesize (stages 8-11)."""

    def __init__(
        self,
        settings: Settings,
        search: SearchPipeline,
        scraper: Scraper,
        gateway: LLMGateway,
        reranker: Reranker,
        cache: CacheClient | None = None,
        embedder: EmbeddingClient | None = None,
    ) -> None:
        self.settings = settings
        self.search = search
        self.scraper = scraper
        self.gateway = gateway
        self.reranker = reranker
        self.cache = cache
        self.embedder = embedder
        self._flights: dict[str, _AnswerFlight] = {}

    async def run(
        self,
        params: SearchParams,
        *,
        model: str | None = None,
        llm_override: LLMOverride | None = None,
        council_mode: bool = False,
        council_endpoints: list[LLMOverride] | None = None,
        domain: str = "auto",
        on_event: OnEvent = None,
    ) -> AsyncIterator[Event]:
        gateway = (
            self.gateway.with_override(llm_override)
            if llm_override is not None and hasattr(self.gateway, "with_override")
            else self.gateway
        )
        default_model = getattr(gateway, "default_quick_model", self.settings.llm_quick_model)
        override_model = llm_override.model if llm_override and llm_override.model else None
        effective_model = override_model or model or default_model
        use_council = council_mode or bool(council_endpoints) or self.settings.llm_council_enabled

        replay = (
            await self._cache_hit(params, effective_model)
            if (llm_override is None and not use_council)
            else None
        )
        if replay is not None:
            async for event in replay:
                yield event
            return

        flight_key = _build_answer_flight_key(
            params,
            model=effective_model,
            llm_override=llm_override,
            council_mode=council_mode,
            council_endpoints=council_endpoints,
            domain=domain,
        )

        # No await between lookup, subscribe and task creation: joining is atomic.
        flight = self._flights.get(flight_key)
        is_first = flight is None
        if flight is None:
            flight = _AnswerFlight()
            self._flights[flight_key] = flight
        queue = flight.subscribe(on_event)
        if is_first:
            flight.task = asyncio.create_task(
                self._produce_answer(
                    flight_key,
                    flight,
                    params,
                    model=effective_model,
                    llm_override=llm_override,
                    council_mode=council_mode,
                    council_endpoints=council_endpoints,
                    domain=domain,
                )
            )

        finished = False
        try:
            while True:
                if is_first:
                    item = await queue.get()
                else:
                    try:
                        item = await asyncio.wait_for(queue.get(), _FLIGHT_IDLE_TIMEOUT_S)
                    except TimeoutError:
                        raise MetaSearchError(
                            ErrorCode.DEADLINE_EXCEEDED,
                            "The shared answer stopped producing output",
                        ) from None
                if item is _FLIGHT_DONE:
                    finished = True
                    break
                if isinstance(item, BaseException):
                    finished = True
                    raise item
                yield item  # type: ignore[misc]
        finally:
            # Runs on normal end, error, cancellation and aclose() alike.
            flight.unsubscribe(queue, on_event)
            task = flight.task
            if not finished and not flight.subscribers and task is not None and not task.done():
                if self._flights.get(flight_key) is flight:
                    del self._flights[flight_key]
                task.cancel()

    async def _produce_answer(
        self,
        flight_key: str,
        flight: _AnswerFlight,
        params: SearchParams,
        *,
        model: str | None = None,
        llm_override: LLMOverride | None = None,
        council_mode: bool = False,
        council_endpoints: list[LLMOverride] | None = None,
        domain: str = "auto",
    ) -> None:
        end: object = _FLIGHT_DONE
        try:
            async for event in self._run_answer(
                params,
                model=model,
                llm_override=llm_override,
                council_mode=council_mode,
                council_endpoints=council_endpoints,
                domain=domain,
                on_event=flight.fan_out,
            ):
                flight.events.append(event)
                flight.publish(event)
        except Exception as exc:
            end = exc
        finally:
            # The flight leaves the table as soon as it ends, whoever is still reading, so a
            # later identical request starts fresh instead of replaying this one.
            if self._flights.get(flight_key) is flight:
                del self._flights[flight_key]
            flight.publish(end)

    async def _run_answer(
        self,
        params: SearchParams,
        *,
        model: str | None = None,
        llm_override: LLMOverride | None = None,
        council_mode: bool = False,
        council_endpoints: list[LLMOverride] | None = None,
        domain: str = "auto",
        on_event: OnEvent = None,
    ) -> AsyncIterator[Event]:
        started = time.perf_counter()
        use_council = council_mode or bool(council_endpoints) or self.settings.llm_council_enabled
        gateway = (
            self.gateway.with_override(llm_override)
            if llm_override is not None and hasattr(self.gateway, "with_override")
            else self.gateway
        )
        default_model = getattr(gateway, "default_quick_model", self.settings.llm_quick_model)
        override_model = llm_override.model if llm_override and llm_override.model else None
        model = override_model or model or default_model

        # run() already looked the answer cache up before starting this flight.
        yield ("status", {"stage": "searching", "detail": params.query})
        outcome = await self.search.run(params, on_event=on_event)
        if not outcome.results:
            raise _no_results(outcome.statuses)

        sources = [
            {"url": r.url, "title": r.title, "domain": r.domain, "score": round(r.score, 4)}
            for r in outcome.results
        ]
        yield ("sources", {"sources": sources})

        yield ("status", {"stage": "scraping", "detail": f"{len(outcome.results)} pages"})
        budget_ctx = get_current_budget()
        scrape_timeout = (
            budget_ctx.stage("scrape", 5.0) if budget_ctx else self.settings.scrape_timeout_s
        )
        try:
            with _stage("scrape"):
                async with asyncio.timeout(scrape_timeout):
                    pages = await self.scraper.scrape_many([r.url for r in outcome.results])
        except TimeoutError:
            pages = [
                PageFailure(url=r.url, status="failed", error="scrape stage deadline exceeded")
                for r in outcome.results
            ]
        ok_pages = [p for p in pages if isinstance(p, PageContent)]

        scored_chunks = await self._score_chunks(
            normalize_query(params.query), outcome.results, ok_pages
        )
        if not scored_chunks:
            raise MetaSearchError(
                ErrorCode.NO_RESULTS,
                "Search returned results but none could be scraped for synthesis",
                details={"engines_status": _status_map(outcome.statuses)},
            )

        chunks = await self._rerank(normalize_query(params.query), scored_chunks)
        included = build_context(
            chunks,
            budget=self.settings.answer_context_budget,
            count=make_counter(self.settings),
        )
        answer_id = uuid.uuid4()

        if use_council:
            from app.llm.council import DreamRSICouncilEngine

            yield (
                "status",
                {
                    "stage": "council_cross_review",
                    "detail": f"Dream-RSI Council & Cross-Review ({len(chunks)} chunks)",
                },
            )
            council_engine = DreamRSICouncilEngine(self.settings, gateway)
            try:
                verdict = await council_engine.run_council(
                    normalize_query(params.query),
                    included,
                    endpoints=council_endpoints,
                    domain=domain,
                )
            except LLMError as exc:
                logger.warning("llm_council_failed", error=str(exc)[:200])
                yield ("error", {"code": "internal", "message": str(exc)[:200]})
                return

            verdict_text = str(verdict.get("verdict_md") or "")
            _CHUNK = 48  # SSE token chunk size (chars)
            for i in range(0, len(verdict_text), _CHUNK):
                yield ("token", {"t": verdict_text[i : i + _CHUNK]})
            citations_payload = list(verdict.get("citations") or [])
            yield (
                "citations",
                {
                    "citations": citations_payload,
                    "invalid_markers": list(verdict.get("invalid_markers") or []),
                },
            )
            council_meta = {
                "domain": verdict.get("domain"),
                "domain_name": verdict.get("domain_name"),
                "chairman_model": verdict.get("chairman_model"),
                "consensus_agreement_score": verdict.get("consensus_agreement_score"),
                "panelists": verdict.get("panelists"),
                "cross_reviews": verdict.get("cross_reviews"),
                "dream_rsi_policy": verdict.get("dream_rsi_policy"),
            }
            yield ("council", council_meta)
            latency_ms = int((time.perf_counter() - started) * 1000)
            # F2: council answers must persist too — same as non-council path.
            council_usage = Usage()
            finalize = asyncio.create_task(
                self._finalize_answer(
                    pages=ok_pages,
                    chunks=chunks,
                    answer_id=answer_id,
                    query_id=outcome.query_id,
                    content_md=verdict_text,
                    model=str(verdict.get("chairman_model") or model),
                    usage=council_usage,
                    latency_ms=latency_ms,
                    citations=[
                        (c.get("n", 0), c.get("url", ""), c.get("title", ""), None)
                        for c in citations_payload
                    ],
                    query=normalize_query(params.query),
                    status="complete",
                )
            )
            _background_tasks.add(finalize)
            finalize.add_done_callback(_log_background)
            yield (
                "done",
                {
                    "answer_id": str(answer_id),
                    "latency_ms": latency_ms,
                    "complete": outcome.complete and bool(ok_pages),
                    "status": "complete",
                    "model": str(verdict.get("chairman_model") or model),
                    "cached": False,
                    "council": council_meta,
                },
            )
            return

        yield ("status", {"stage": "synthesizing", "detail": f"{len(chunks)} chunks"})
        messages = build_messages(normalize_query(params.query), included)

        text_parts: list[str] = []
        usage = Usage()
        finish_reasons: set[str] = set()
        try:
            async for delta in gateway.stream(messages, model=model):
                if delta.text:
                    text_parts.append(delta.text)
                    yield ("token", {"t": delta.text})
                if delta.finish_reason:
                    finish_reasons.add(delta.finish_reason)
                if delta.usage:
                    usage.input_tokens += delta.usage.input_tokens
                    usage.output_tokens += delta.usage.output_tokens
        except LLMError as exc:
            logger.warning("llm_stream_failed", error=str(exc)[:200])
            error: dict[str, object] = {
                "code": getattr(exc, "code", "internal"),
                "message": str(exc)[:200],
            }
            if getattr(exc, "retry_after_s", None) is not None:
                error["retry_after_s"] = round(float(exc.retry_after_s), 1)
            yield ("error", error)
            return
        record_usage(model, usage)
        # DAT-07: a "length" cut-off is not a complete answer.
        answer_status = "truncated" if "length" in finish_reasons else "complete"

        check = validate_citations("".join(text_parts), included)
        latency_ms = int((time.perf_counter() - started) * 1000)
        citations_payload = [
            {"n": c.n, "url": c.url, "title": c.title, "chunk_id": c.chunk_id}
            for c in check.citations
        ]

        yield (
            "citations",
            {"citations": citations_payload, "invalid_markers": check.invalid_markers},
        )

        # REL-04: Cache write and DB finalize MUST be executed/scheduled before done event
        # so if the client disconnects immediately on done, cache and storage are never lost!
        is_complete = outcome.complete and bool(ok_pages) and answer_status == "complete"
        if (
            self.cache is not None
            and llm_override is None
            and not use_council
            and answer_status == "complete"
            and is_complete
        ):
            try:
                await self._cache_store(
                    params, model, answer_id, check.text, citations_payload, sources
                )
            except Exception as exc:
                logger.warning("answer_cache_store_failed", error=type(exc).__name__)

        finalize = asyncio.create_task(
            self._finalize_answer(
                pages=ok_pages,
                chunks=chunks,
                answer_id=answer_id,
                query_id=outcome.query_id,
                content_md=check.text,
                model=model,
                usage=usage,
                latency_ms=latency_ms,
                citations=[(c.n, c.url, c.title, c.chunk_id) for c in check.citations],
                query=normalize_query(params.query),
                status=answer_status,
                cacheable=not _has_domain_filter(params),
            )
        )
        _background_tasks.add(finalize)
        finalize.add_done_callback(_log_background)

        yield (
            "done",
            {
                "answer_id": str(answer_id),
                "latency_ms": latency_ms,
                "complete": outcome.complete and bool(ok_pages) and answer_status == "complete",
                "status": answer_status,
                "model": model,
                "cached": False,
            },
        )

    async def prepare_context(
        self,
        params: SearchParams,
        *,
        budget: int | None = None,
        domain: str = "auto",
        include: list[str] | None = None,
    ) -> dict[str, object]:
        """Run stages 3-10 (Search -> Scrape -> Chunk -> Rerank -> Context Pack)
        without calling an internal LLM.

        Designed for external systems that already have their own LLM and want
        MetaSearch to act as their institutional RAG & multi-domain context engine.
        """
        from app.retrieval.domain_intelligence import build_domain_analysis_preview

        started = time.perf_counter()
        normalized = normalize_query(params.query)
        domain_info = build_domain_analysis_preview(normalized, requested_domain=domain)
        inferred_cats = sorted(
            set(params.categories or ["general"])
            | infer_categories_from_query(normalized, news=params.news)
        )
        outcome = await self.search.run(params)
        if not outcome.results:
            raise _no_results(outcome.statuses)

        sources = [
            {
                "url": r.url,
                "title": r.title,
                "domain": r.domain,
                "score": round(r.score, 4),
                **_data_marks(r),
            }
            for r in outcome.results
        ]
        budget_ctx = get_current_budget()
        scrape_timeout = (
            budget_ctx.stage("scrape", 6.0) if budget_ctx else self.settings.scrape_timeout_s
        )
        try:
            with _stage("scrape"):
                async with asyncio.timeout(scrape_timeout):
                    pages = await self.scraper.scrape_many([r.url for r in outcome.results])
        except TimeoutError:
            pages = [
                PageFailure(url=r.url, status="failed", error="scrape stage deadline exceeded")
                for r in outcome.results
            ]
        ok_pages = [p for p in pages if isinstance(p, PageContent)]

        scored_chunks = await self._score_chunks(normalized, outcome.results, ok_pages)
        if not scored_chunks:
            raise MetaSearchError(
                ErrorCode.NO_RESULTS,
                "Search returned results but none could be scraped for context preparation",
                details={"engines_status": _status_map(outcome.statuses)},
            )

        chunks = await self._rerank(normalized, scored_chunks)
        eff_budget = budget or self.settings.answer_context_budget
        included = build_context(
            chunks,
            budget=eff_budget,
            count=make_counter(self.settings),
        )
        context_md = render_context(included)
        messages = build_messages(normalized, included)
        latency_ms = int((time.perf_counter() - started) * 1000)

        by_url: dict[str, ScoredResult] = {r.url: r for r in outcome.results}
        page_by_url: dict[str, PageContent] = {}
        for page in ok_pages:
            page_by_url[page.url] = page
            page_by_url.setdefault(page.final_url, page)

        def chunk_marks(url: str) -> dict[str, object]:
            result = by_url.get(url)
            page = page_by_url.get(url)
            marks = _data_marks(result) if result is not None else dict(_EMPTY_MARKS)
            # A chunk read from a fetched page is verifiable at that page; a snippet-only
            # chunk has no fetch time and keeps whatever the engine reported.
            if page is not None:
                marks["source_url"] = marks["source_url"] or page.final_url
                marks["fetched_at"] = _iso(page.fetched_at)
            return marks

        wanted = set(include or _DEFAULT_CONTEXT_INCLUDE)
        payload: dict[str, object] = {
            "query_id": str(outcome.query_id),
            "normalized_query": normalized,
            "inferred_categories": inferred_cats,
            "domain_intelligence": domain_info,
            "context_md": context_md,
            "system_prompt": SYSTEM_PROMPT,
            "messages": messages,
            "chunks": [
                {
                    "n": n,
                    "chunk_id": chunk.id,
                    "url": chunk.document_url,
                    "title": chunk.source_title,
                    "heading_path": chunk.heading_path,
                    "content": chunk.content,
                    "word_count": chunk.word_count,
                    **chunk_marks(chunk.document_url),
                }
                for n, chunk in included
            ],
            "sources": sources,
            "engines_status": [
                {
                    "engine": s.engine,
                    "status": s.status,
                    "results": s.results,
                    "ms": s.ms,
                    "error": s.error,
                }
                for s in outcome.statuses
            ],
            "latency_ms": latency_ms,
            "complete": outcome.complete and bool(ok_pages),
        }
        for part in _OPTIONAL_CONTEXT_PARTS - wanted:
            payload.pop(part, None)
        return payload

    async def _finalize_answer(
        self,
        *,
        pages: list[PageContent],
        chunks: list[Chunk],
        answer_id: uuid.UUID,
        query_id: uuid.UUID,
        content_md: str,
        model: str,
        usage: Usage,
        latency_ms: int,
        citations: list[tuple[int, str, str, str | None]],
        query: str,
        status: str = "complete",
        cacheable: bool = True,
    ) -> None:
        if pages:
            await persist.persist_pages_async(pages, chunks)
        await persist.persist_answer(
            answer_id=answer_id,
            query_id=query_id,
            content_md=content_md,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd(model),
            latency_ms=latency_ms,
            citations=citations,
            status=status,
        )
        if cacheable and self.embedder is not None and self.embedder.enabled:
            await self._semantic_cache_store(query, answer_id, model)

    # -- rerank ------------------------------------------------------------

    async def _rerank(self, query: str, scored_chunks: list[tuple[float, Chunk]]) -> list[Chunk]:
        try:
            with _stage("rerank"):
                return await self.reranker.rerank(query, scored_chunks)
        except RerankerUnavailable as exc:
            logger.warning("rerank_fallback_heuristic", reason=str(exc)[:120])
            return heuristic_order(scored_chunks)

    async def _score_chunks(
        self, query: str, results: list[ScoredResult], pages: list[PageContent]
    ) -> list[tuple[float, Chunk]]:
        return await run_cpu(_score_chunks_sync, query, results, pages)

    # -- answer cache (L1 + L2) ---------------------------------------------

    async def _cache_hit(self, params: SearchParams, model: str) -> AsyncIterator[Event] | None:
        """Return a replay generator when L1 or L2 has a fresh answer."""
        if _has_domain_filter(params):
            return None  # the answer cache is keyed without domain filters
        query = normalize_query(params.query)
        if self.cache is not None:
            payload = await self.cache.get_json(
                answer_cache_key(
                    query,
                    model=model,
                    lang=params.lang,
                    k=params.k,
                    categories=params.categories,
                    freshness_days=params.freshness_days,
                    engines=params.engines,
                    news=params.news,
                    deep=params.deep,
                )
            )
            if payload and payload.get("answer_md"):
                CACHE_EVENTS.labels(kind="answer", outcome="hit").inc()
                return self._replay(payload)
            CACHE_EVENTS.labels(kind="answer", outcome="miss").inc()
        payload = await self._semantic_lookup(query)
        if payload is not None:
            CACHE_EVENTS.labels(kind="semantic", outcome="hit").inc()
            return self._replay(payload)
        return None

    def _replay(self, payload: dict) -> AsyncIterator[Event]:
        async def _gen() -> AsyncIterator[Event]:
            yield ("sources", {"sources": payload.get("sources") or []})
            text = str(payload.get("answer_md") or "")
            for i in range(0, len(text), 24):
                yield ("token", {"t": text[i : i + 24]})
            yield (
                "citations",
                {"citations": payload.get("citations") or [], "invalid_markers": []},
            )
            if payload.get("council"):
                yield ("council", payload["council"])
            yield (
                "done",
                {
                    "answer_id": str(payload.get("answer_id") or uuid.uuid4()),
                    "latency_ms": 0,
                    "status": str(payload.get("status") or "complete"),
                    "model": str(payload.get("model") or ""),
                    "cached": True,
                    "complete": bool(payload.get("complete", True)),
                    **({"council": payload["council"]} if payload.get("council") else {}),
                },
            )

        return _gen()

    async def _cache_store(
        self,
        params: SearchParams,
        model: str,
        answer_id: uuid.UUID,
        answer_md: str,
        citations: list[dict],
        sources: list[dict],
    ) -> None:
        if self.cache is None or not answer_md or _has_domain_filter(params):
            return
        categories = set(params.categories or [])
        if categories == {"general"} or params.news:
            categories |= infer_categories_from_query(params.query, news=params.news)
        if "financial" in categories and getattr(params, "max_age_s", None) is None:
            # DAT-10: Dynamic financial answers must not be cached without max_age_s
            return
        await self.cache.set_json(
            answer_cache_key(
                normalize_query(params.query),
                model=model,
                lang=params.lang,
                k=params.k,
                categories=params.categories,
                freshness_days=params.freshness_days,
                engines=params.engines,
                news=params.news,
                deep=params.deep,
            ),
            {
                "answer_id": str(answer_id),
                "cached_at": time.time(),
                "answer_md": answer_md,
                "citations": citations,
                "sources": sources,
                "model": model,
                "status": "complete",
                "complete": True,
            },
            ttl_s=self.settings.cache_answer_ttl_s,
        )

    async def _semantic_lookup(self, query: str) -> dict | None:
        """L2: pgvector cosine > threshold on the semantic_cache table."""
        if (
            self.embedder is None
            or not self.embedder.enabled
            or self.settings.database_url.startswith("sqlite")
        ):
            return None
        try:
            vector = await self.embedder.embed(query)
        except EmbeddingError as exc:
            logger.warning("semantic_lookup_skipped", reason=str(exc)[:120])
            return None
        max_distance = 1.0 - self.settings.semantic_cache_threshold
        try:
            async with get_sessionmaker()() as session:
                row = (
                    await session.execute(
                        select(SemanticCache.answer_id, Answer.content_md, Answer.model)
                        .join(Answer, Answer.id == SemanticCache.answer_id)
                        .where(
                            SemanticCache.query_embedding.cosine_distance(vector) <= max_distance,
                            SemanticCache.expires_at > func.now(),
                        )
                        .order_by(SemanticCache.query_embedding.cosine_distance(vector))
                        .limit(1)
                    )
                ).first()
            if row is None or not row[1]:
                CACHE_EVENTS.labels(kind="semantic", outcome="miss").inc()
                return None
        except Exception as exc:
            logger.warning("semantic_lookup_failed", error=type(exc).__name__)
            return None
        answer_id, content_md, model = row
        if self.cache is not None:
            cached = await self.cache.get_json(
                answer_cache_key(query, model=model or "", lang=None)
            )  # L2 replay: original search params unknown, default key shape
            if cached and cached.get("answer_md"):
                return cached
        # No L1 payload for this answer: rebuild citations from the DB so the
        # replayed answer still carries its sources.
        citations = await persist.load_citations(answer_id)
        return {
            "answer_id": str(answer_id),
            "answer_md": content_md,
            "citations": citations,
            "sources": [
                {"url": c["url"], "title": c["title"], "domain": _domain(c["url"])}
                for c in citations
            ],
            "model": model or "",
        }

    async def _semantic_cache_store(self, query: str, answer_id: uuid.UUID, model: str) -> None:
        if (
            self.embedder is None
            or not self.embedder.enabled
            or self.settings.database_url.startswith("sqlite")
        ):
            return
        try:
            vector = await self.embedder.embed(query)
            if len(vector) != EMBEDDING_DIM:
                return
            async with get_sessionmaker()() as session:
                session.add(
                    SemanticCache(
                        query_text=query,
                        query_embedding=vector,
                        answer_id=answer_id,
                        model=model,
                        expires_at=datetime.now(UTC) + timedelta(days=7),
                    )
                )
                await session.commit()
        except Exception as exc:
            logger.warning("semantic_cache_write_failed", error=type(exc).__name__)


def _log_background(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    if task.done() and not task.cancelled():
        exc = task.exception()
        if exc is not None:
            logger.warning("background_task_failed", error=type(exc).__name__)


def _no_results(statuses: list[EngineStatus]) -> MetaSearchError:
    return MetaSearchError(
        ErrorCode.NO_RESULTS,
        "All engines failed or returned no results",
        details={"engines_status": _status_map(statuses)},
    )


def _status_map(statuses: list[EngineStatus]) -> dict[str, str]:
    return {s.engine: s.status for s in statuses}


def _domain(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").lower()
