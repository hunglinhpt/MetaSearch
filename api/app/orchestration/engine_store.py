"""Engine config store: hot config + health tracking (docs/06-api-spec.md §5).

Two implementations behind one protocol:
- ``InMemoryEngineStore``: source of truth when the database is unavailable;
  also used directly by unit tests.
- ``DbEngineStore``: persists to the ``engines`` / ``engine_health`` tables and
  falls back to an embedded in-memory store on any database error, so a dead
  Postgres never breaks search.

Auto-disable: ``engine_auto_disable_threshold`` consecutive failures flip
``enabled=false`` with an ``auto:`` reason; the worker recovery probe re-enables
such engines when a probe succeeds.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.models import Engine as EngineRow
from app.db.models import EngineHealth as EngineHealthRow
from app.db.session import get_sessionmaker
from app.retrieval.models import EngineConfig

logger = get_logger(__name__)

# import path stored in the engines.adapter column (docs/03 §2)
ADAPTER_PATHS = {
    "searxng": "app.retrieval.adapters.searxng:SearXNGAdapter",
    "ddg": "app.retrieval.adapters.ddg:DuckDuckGoAdapter",
    "tavily": "app.retrieval.adapters.tavily:TavilyAdapter",
    "serper": "app.retrieval.adapters.serper:SerperAdapter",
    "brave": "app.retrieval.adapters.brave:BraveAdapter",
    "arxiv": "app.retrieval.adapters.arxiv:ArxivAdapter",
    "wikipedia": "app.retrieval.adapters.wikipedia:WikipediaAdapter",
    "stackoverflow": "app.retrieval.adapters.stackoverflow:StackOverflowAdapter",
    "reddit": "app.retrieval.adapters.reddit:RedditAdapter",
    "github": "app.retrieval.adapters.github:GitHubAdapter",
    "pubmed": "app.retrieval.adapters.pubmed:PubMedAdapter",
    "huggingface": "app.retrieval.adapters.huggingface:HuggingFaceAdapter",
    "semanticscholar": "app.retrieval.adapters.semanticscholar:SemanticScholarAdapter",
    "yahoo_finance": "app.retrieval.adapters.yahoo_finance:YahooFinanceAdapter",
    "sec_edgar": "app.retrieval.adapters.sec_edgar:SecEdgarAdapter",
    "stooq": "app.retrieval.adapters.stooq:StooqFinanceAdapter",
    "google_news": "app.retrieval.adapters.google_news:GoogleNewsAdapter",
    "fengshui": "app.retrieval.adapters.fengshui:FengShuiAdapter",
    "viet_stock": "app.retrieval.adapters.viet_stock:VietStockAdapter",
}


@dataclass(slots=True)
class EngineHealthSummary:
    engine_id: str
    enabled: bool
    disabled_reason: str | None
    calls: int = 0
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0


class EngineStore(Protocol):
    async def snapshot(self) -> dict[str, EngineConfig]: ...
    async def update(self, engine_id: str, fields: dict[str, Any]) -> EngineConfig | None: ...
    async def record_result(self, engine_id: str, *, ok: bool, latency_ms: int) -> None: ...
    async def health(self) -> dict[str, EngineHealthSummary]: ...
    async def auto_disabled(self) -> list[str]: ...


def default_configs(settings: Settings) -> dict[str, EngineConfig]:
    """Env-derived defaults shared by both stores (docs/03 §2 Engine Registry)."""
    return {
        "searxng": EngineConfig(
            id="searxng",
            name="SearXNG (70+ engines)",
            category="general",
            weight=1.5,
            enabled=bool(settings.searxng_enabled),
            timeout_ms=int(settings.searxng_timeout_s * 1000),
            max_results=15,
            requires_key=False,
            adapter_path=ADAPTER_PATHS["searxng"],
        ),
        "ddg": EngineConfig(
            id="ddg",
            name="DuckDuckGo (keyless, Lite profile)",
            category="general",
            weight=1.0,
            enabled=True,
            timeout_ms=4000,
            max_results=15,
            requires_key=False,
            adapter_path=ADAPTER_PATHS["ddg"],
        ),
        "tavily": EngineConfig(
            id="tavily",
            name="Tavily API",
            category="general",
            weight=1.0,
            enabled=bool(settings.tavily_api_key),
            timeout_ms=3000,
            max_results=15,
            requires_key=True,
            adapter_path=ADAPTER_PATHS["tavily"],
        ),
        "serper": EngineConfig(
            id="serper",
            name="Serper.dev (Google)",
            category="general",
            weight=1.0,
            enabled=bool(settings.serper_api_key),
            timeout_ms=3000,
            max_results=15,
            requires_key=True,
            adapter_path=ADAPTER_PATHS["serper"],
        ),
        "brave": EngineConfig(
            id="brave",
            name="Brave Search API",
            category="general",
            weight=1.0,
            enabled=bool(settings.brave_api_key),
            timeout_ms=3000,
            max_results=15,
            requires_key=True,
            adapter_path=ADAPTER_PATHS["brave"],
        ),
        # Verticals (docs/01 FR-3): no key; join the fan-out only when the
        # request asks for their category (SearchPipeline._fanout).
        "arxiv": EngineConfig(
            id="arxiv",
            name="arXiv (academic)",
            category="academic",
            weight=1.0,
            enabled=True,
            timeout_ms=5000,
            max_results=10,
            requires_key=False,
            adapter_path=ADAPTER_PATHS["arxiv"],
        ),
        "wikipedia": EngineConfig(
            id="wikipedia",
            name="Wikipedia (reference)",
            category="reference",
            weight=1.0,
            enabled=True,
            timeout_ms=3000,
            max_results=10,
            requires_key=False,
            adapter_path=ADAPTER_PATHS["wikipedia"],
        ),
        "stackoverflow": EngineConfig(
            id="stackoverflow",
            name="Stack Overflow (developer)",
            category="developer",
            weight=1.0,
            enabled=True,
            timeout_ms=4000,
            max_results=10,
            requires_key=False,
            adapter_path=ADAPTER_PATHS["stackoverflow"],
        ),
        "reddit": EngineConfig(
            id="reddit",
            name="Reddit (social)",
            category="social",
            weight=0.9,
            enabled=True,
            timeout_ms=4000,
            max_results=10,
            requires_key=False,
            adapter_path=ADAPTER_PATHS["reddit"],
        ),
        "github": EngineConfig(
            id="github",
            name="GitHub Repositories (developer)",
            category="developer",
            weight=1.1,
            enabled=True,
            timeout_ms=4000,
            max_results=10,
            requires_key=False,
            adapter_path=ADAPTER_PATHS["github"],
        ),
        "pubmed": EngineConfig(
            id="pubmed",
            name="PubMed Central (academic)",
            category="academic",
            weight=1.1,
            enabled=True,
            timeout_ms=5000,
            max_results=10,
            requires_key=False,
            adapter_path=ADAPTER_PATHS["pubmed"],
        ),
        "huggingface": EngineConfig(
            id="huggingface",
            name="HuggingFace Models (developer)",
            category="developer",
            weight=1.0,
            enabled=True,
            timeout_ms=4000,
            max_results=10,
            requires_key=False,
            adapter_path=ADAPTER_PATHS["huggingface"],
        ),
        "semanticscholar": EngineConfig(
            id="semanticscholar",
            name="Semantic Scholar (academic)",
            category="academic",
            weight=1.0,
            enabled=True,
            timeout_ms=4000,
            max_results=10,
            requires_key=False,
            adapter_path=ADAPTER_PATHS["semanticscholar"],
        ),
        "yahoo_finance": EngineConfig(
            id="yahoo_finance",
            name="Yahoo Finance (financial)",
            category="financial",
            weight=1.2,
            enabled=True,
            timeout_ms=4000,
            max_results=10,
            requires_key=False,
            adapter_path=ADAPTER_PATHS["yahoo_finance"],
        ),
        "sec_edgar": EngineConfig(
            id="sec_edgar",
            name="SEC EDGAR Filings (financial)",
            category="financial",
            weight=1.1,
            enabled=True,
            timeout_ms=5000,
            max_results=10,
            requires_key=False,
            adapter_path=ADAPTER_PATHS["sec_edgar"],
        ),
        "stooq": EngineConfig(
            id="stooq",
            name="Stooq Global Markets & Yields (financial)",
            category="financial",
            weight=1.2,
            enabled=True,
            timeout_ms=4000,
            max_results=10,
            requires_key=False,
            adapter_path=ADAPTER_PATHS["stooq"],
        ),
        "google_news": EngineConfig(
            id="google_news",
            name="Google News RSS (news)",
            category="news",
            weight=1.2,
            enabled=True,
            timeout_ms=4000,
            max_results=15,
            requires_key=False,
            adapter_path=ADAPTER_PATHS["google_news"],
        ),
        "viet_stock": EngineConfig(
            id="viet_stock",
            name="Vietnam Stocks HSX/HNX Deep Intelligence (financial)",
            category="financial",
            weight=1.5,
            enabled=True,
            timeout_ms=3500,
            max_results=12,
            requires_key=False,
            adapter_path=ADAPTER_PATHS["viet_stock"],
        ),
        "fengshui": EngineConfig(
            id="fengshui",
            name="Feng Shui & Metaphysics (fengshui)",
            category="fengshui",
            weight=1.2,
            enabled=True,
            timeout_ms=3000,
            max_results=10,
            requires_key=False,
            adapter_path=ADAPTER_PATHS["fengshui"],
        ),
    }


def _clear_auto_state(cfg: EngineConfig) -> EngineConfig:
    return replace(cfg, enabled=True, disabled_reason=None)


async def clear_legacy_auto_disabled(sessionmaker_provider=None) -> int:
    sm = sessionmaker_provider or get_sessionmaker()
    try:
        async with sm() as session:
            result = await session.execute(
                update(EngineRow)
                .where(EngineRow.disabled_reason.like("auto:%"))
                .values(enabled=True, disabled_reason=None)
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0)
    except Exception as exc:
        logger.warning("clear_legacy_auto_disabled_failed", error=str(exc))
        return 0


class InMemoryEngineStore:
    """Process-local store; doubles as the fallback backend of DbEngineStore."""

    def __init__(self, configs: dict[str, EngineConfig]) -> None:
        self._configs: dict[str, EngineConfig] = dict(configs)
        self._failures: dict[str, int] = {}
        self._windows: dict[str, dict[str, int]] = {}

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> InMemoryEngineStore:
        return cls(default_configs(settings or get_settings()))

    async def snapshot(self) -> dict[str, EngineConfig]:
        return dict(self._configs)

    def clear_legacy_auto_disabled(self) -> int:
        count = 0
        for eid, cfg in list(self._configs.items()):
            if cfg.disabled_reason and cfg.disabled_reason.startswith("auto:"):
                self._configs[eid] = replace(cfg, enabled=True, disabled_reason=None)
                count += 1
        return count

    async def update(self, engine_id: str, fields: dict[str, Any]) -> EngineConfig | None:
        cfg = self._configs.get(engine_id)
        if cfg is None:
            return None
        allowed = {"enabled", "weight", "timeout_ms", "max_results"}
        changes = {k: v for k, v in fields.items() if k in allowed and v is not None}
        updated = replace(cfg, **changes)
        if changes.get("enabled"):
            updated = _clear_auto_state(updated)
            self._failures[engine_id] = 0
        self._configs[engine_id] = updated
        return updated

    async def record_result(self, engine_id: str, *, ok: bool, latency_ms: int) -> None:
        window = self._windows.setdefault(engine_id, {"calls": 0, "successes": 0, "failures": 0})
        window["calls"] += 1
        window["successes" if ok else "failures"] += 1

        consecutive = 0 if ok else self._failures.get(engine_id, 0) + 1
        self._failures[engine_id] = consecutive

    async def health(self) -> dict[str, EngineHealthSummary]:
        out: dict[str, EngineHealthSummary] = {}
        for engine_id, cfg in self._configs.items():
            window = self._windows.get(engine_id, {})
            out[engine_id] = EngineHealthSummary(
                engine_id=engine_id,
                enabled=cfg.enabled,
                disabled_reason=cfg.disabled_reason,
                calls=window.get("calls", 0),
                successes=window.get("successes", 0),
                failures=window.get("failures", 0),
                consecutive_failures=self._failures.get(engine_id, 0),
            )
        return out

    async def auto_disabled(self) -> list[str]:
        return [
            engine_id
            for engine_id, cfg in self._configs.items()
            if not cfg.enabled and (cfg.disabled_reason or "").startswith("auto:")
        ]


class DbEngineStore:
    """Postgres-backed store with an in-memory fallback and a short TTL cache.

    Every DB operation is bounded by ``engine_store_db_timeout_s``: a
    partitioned (not refused) Postgres otherwise parks each request for the
    full asyncpg connect timeout — measured at 3s per op on the perf rig,
    30s+ once the pool saturates. On timeout we degrade to the fallback store.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        fallback: InMemoryEngineStore | None = None,
        *,
        refresh_s: float | None = None,
        session_factory: Any | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._fallback = fallback or InMemoryEngineStore.from_settings(self._settings)
        self._refresh_s = refresh_s if refresh_s is not None else self._settings.registry_refresh_s
        self._db_timeout_s = self._settings.engine_store_db_timeout_s
        self._cache: dict[str, EngineConfig] | None = None
        self._cache_at = 0.0
        self._db_warned = False
        self._session_factory = session_factory
        self._db_error_until = 0.0
        self._refresh_task: asyncio.Task[None] | None = None

    def _get_sm(self):
        return self._session_factory if self._session_factory is not None else get_sessionmaker()

    async def _guarded(self, coro):
        """Bound one DB operation; fall back to memory when it stalls."""
        try:
            return await asyncio.wait_for(coro, timeout=self._db_timeout_s)
        except Exception:
            if not self._db_warned:
                logger.warning("engine_store_db_timeout", timeout_s=self._db_timeout_s)
                self._db_warned = True
            raise

    # -- snapshot ----------------------------------------------------------

    async def snapshot(self) -> dict[str, EngineConfig]:
        now = time.monotonic()
        if self._cache is None:
            if now < self._db_error_until:
                return await self._fallback.snapshot()
            try:
                rows = await self._guarded(self._load_rows())
                if not rows:
                    await self._guarded(self._seed_rows())
                    rows = await self._guarded(self._load_rows())
                self._cache = rows
                self._cache_at = time.monotonic()
                self._db_error_until = 0.0
                return dict(rows)
            except Exception as exc:
                if not self._db_warned:
                    logger.warning("engine_store_db_unavailable", error=type(exc).__name__)
                    self._db_warned = True
                self._db_error_until = time.monotonic() + 30.0
                fb = await self._fallback.snapshot()
                self._cache = fb
                self._cache_at = time.monotonic()
                return dict(fb)

        # Stale-while-revalidate: return cache immediately and refresh in background
        if (
            now - self._cache_at >= self._refresh_s
            and now >= self._db_error_until
            and (self._refresh_task is None or self._refresh_task.done())
        ):
            self._refresh_task = asyncio.create_task(self._revalidate_background())

        return dict(self._cache)

    async def _revalidate_background(self) -> None:
        try:
            rows = await self._guarded(self._load_rows())
            if rows:
                self._cache = rows
                self._cache_at = time.monotonic()
                self._db_error_until = 0.0
        except Exception:
            self._db_error_until = time.monotonic() + 30.0
            self._cache_at = time.monotonic()

    async def _load_rows(self) -> dict[str, EngineConfig]:
        async with self._get_sm()() as session:
            rows = (await session.execute(select(EngineRow))).scalars().all()
        return {
            row.id: EngineConfig(
                id=row.id,
                name=row.name,
                category=row.category,
                weight=float(row.weight),
                enabled=bool(row.enabled),
                timeout_ms=int(row.timeout_ms),
                max_results=int(row.max_results),
                requires_key=bool(row.requires_key),
                adapter_path=row.adapter,
                disabled_reason=row.disabled_reason,
            )
            for row in rows
        }

    async def _seed_rows(self) -> None:
        sm = self._get_sm()
        await clear_legacy_auto_disabled(sm)
        async with sm() as session:
            insert = _dialect_insert(session)
            for cfg in default_configs(self._settings).values():
                await session.execute(
                    insert(EngineRow)
                    .values(
                        id=cfg.id,
                        name=cfg.name,
                        category=cfg.category,
                        adapter=cfg.adapter_path,
                        weight=cfg.weight,
                        enabled=cfg.enabled,
                        timeout_ms=cfg.timeout_ms,
                        max_results=cfg.max_results,
                        requires_key=cfg.requires_key,
                    )
                    .on_conflict_do_nothing(index_elements=["id"])
                )
            await session.commit()

    # -- mutations -----------------------------------------------------------

    async def update(self, engine_id: str, fields: dict[str, Any]) -> EngineConfig | None:
        allowed = {"enabled", "weight", "timeout_ms", "max_results"}
        changes: dict[str, Any] = {
            k: v for k, v in fields.items() if k in allowed and v is not None
        }
        if not changes:
            return (await self.snapshot()).get(engine_id)
        if changes.get("enabled") is True:
            # Re-enabling (manually or via the recovery probe) clears the
            # auto-disable state, mirroring InMemoryEngineStore.update.
            changes["disabled_reason"] = None
            changes["consecutive_failures"] = 0

        async def _do_update() -> None:
            async with self._get_sm()() as session:
                await session.execute(
                    _dialect_insert(session)(EngineRow)
                    .values(id=engine_id, **self._row_values(engine_id, changes))
                    .on_conflict_do_update(index_elements=["id"], set_=changes)
                )
                await session.commit()

        try:
            await self._guarded(_do_update())
        except Exception as exc:
            logger.warning("engine_update_db_failed", error=type(exc).__name__)
            return await self._fallback.update(engine_id, changes)
        self._cache = None
        return (await self.snapshot()).get(engine_id)

    def _row_values(self, engine_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Full column set for the insert arm of the upsert."""
        defaults = default_configs(self._settings).get(engine_id)
        base: dict[str, Any] = {
            "name": defaults.name if defaults else engine_id,
            "category": defaults.category if defaults else "general",
            "adapter": defaults.adapter_path if defaults else "",
            "weight": 1.0,
            "enabled": True,
            "timeout_ms": 3000,
            "max_results": 15,
            "requires_key": False,
        }
        base.update(changes)
        return base

    async def record_result(self, engine_id: str, *, ok: bool, latency_ms: int) -> None:
        await self._fallback.record_result(engine_id, ok=ok, latency_ms=latency_ms)

    async def _record_db(self, engine_id: str, *, ok: bool) -> bool:
        window_start = _window_start()
        state_changed = False
        async with self._get_sm()() as session:
            row = (
                await session.execute(
                    select(EngineRow.consecutive_failures, EngineRow.enabled).where(
                        EngineRow.id == engine_id
                    )
                )
            ).first()
            if row is None:
                # Ensure row exists before updating health/failure counter
                defaults = default_configs(self._settings).get(engine_id)
                if defaults is not None:
                    await session.execute(
                        _dialect_insert(session)(EngineRow)
                        .values(
                            id=defaults.id,
                            name=defaults.name,
                            category=defaults.category,
                            adapter=defaults.adapter_path,
                            weight=defaults.weight,
                            enabled=defaults.enabled,
                            timeout_ms=defaults.timeout_ms,
                            max_results=defaults.max_results,
                            requires_key=defaults.requires_key,
                            consecutive_failures=0,
                        )
                        .on_conflict_do_nothing(index_elements=["id"])
                    )
                    row = (0, defaults.enabled)

            prev_consecutive = int((row[0] if row else 0) or 0)
            consecutive = 0 if ok else prev_consecutive + 1

            await session.execute(
                _dialect_insert(session)(EngineHealthRow)
                .values(
                    engine_id=engine_id,
                    window_start=window_start,
                    calls=1,
                    successes=1 if ok else 0,
                    failures=0 if ok else 1,
                    consecutive_failures=consecutive,
                )
                .on_conflict_do_update(
                    index_elements=["engine_id", "window_start"],
                    set_={
                        "calls": EngineHealthRow.calls + 1,
                        "successes": EngineHealthRow.successes + (1 if ok else 0),
                        "failures": EngineHealthRow.failures + (0 if ok else 1),
                        "consecutive_failures": consecutive,
                    },
                )
            )
            if consecutive != prev_consecutive:
                await session.execute(
                    update(EngineRow)
                    .where(EngineRow.id == engine_id)
                    .values(consecutive_failures=consecutive)
                )
            await session.commit()
        return state_changed

    async def _load_health(self):
        async with self._get_sm()() as session:
            engines = (await session.execute(select(EngineRow))).scalars().all()
            windows = (
                (
                    await session.execute(
                        select(EngineHealthRow).where(
                            EngineHealthRow.window_start == _window_start()
                        )
                    )
                )
                .scalars()
                .all()
            )
        return engines, windows

    async def health(self) -> dict[str, EngineHealthSummary]:
        try:
            engines, windows = await self._guarded(self._load_health())
        except Exception as exc:
            logger.warning("engine_health_db_failed", error=type(exc).__name__)
            return await self._fallback.health()
        by_engine = {w.engine_id: w for w in windows}
        out: dict[str, EngineHealthSummary] = {}
        for row in engines:
            window = by_engine.get(row.id)
            out[row.id] = EngineHealthSummary(
                engine_id=row.id,
                enabled=bool(row.enabled),
                disabled_reason=row.disabled_reason,
                calls=int(window.calls) if window else 0,
                successes=int(window.successes) if window else 0,
                failures=int(window.failures) if window else 0,
                consecutive_failures=int(row.consecutive_failures or 0),
            )
        return out

    async def _load_auto_disabled(self) -> list[str]:
        async with self._get_sm()() as session:
            result = await session.execute(
                select(EngineRow.id).where(
                    EngineRow.enabled.is_(False),
                    EngineRow.disabled_reason.like("auto:%"),
                )
            )
            return [row[0] for row in result.all()]

    async def auto_disabled(self) -> list[str]:
        try:
            return await self._guarded(self._load_auto_disabled())
        except Exception:
            return await self._fallback.auto_disabled()

    def _invalidate(self) -> None:
        self._cache = None


def _dialect_insert(session):
    """Postgres and SQLite both support ON CONFLICT — pick the right insert."""
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        return sqlite_insert
    return pg_insert


def _window_start() -> datetime:
    """Health rows aggregate per minute."""
    now = datetime.now(UTC)
    return now.replace(second=0, microsecond=0)
