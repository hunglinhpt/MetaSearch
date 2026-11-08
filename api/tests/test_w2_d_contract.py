"""Wave 2 package D: the full /v1 contract, through the real routes."""

from __future__ import annotations

import asyncio
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.api import deps
from app.cache import CacheClient, _local_cache
from app.config import get_settings
from app.db.bootstrap import ensure_schema
from app.db.session import dispose_engine
from app.extraction.scraper import PageContent, PageFailure
from app.llm.gateway import StreamDelta, Usage
from app.main import app
from app.orchestration.engine_store import InMemoryEngineStore
from app.retrieval.models import EngineConfig, RawResult
from app.retrieval.reranker import HeuristicReranker

_DEPS_SWAPPED = (
    "get_cache",
    "get_engine_store",
    "build_adapters",
    "get_scraper",
    "get_gateway",
    "get_embedder",
    "get_reranker",
)
FETCHED = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)


def _raw(url: str, title: str, snippet: str, **marks) -> RawResult:
    return RawResult(
        url=url, title=title, snippet=snippet, adapter="fake", source_engines=[], rank=0, **marks
    )


def _page(url: str, title: str) -> PageContent:
    body = " ".join(["PostgreSQL vacuum btree planner statistics index tuning."] * 30)
    return PageContent(
        url=url,
        final_url=url,
        title=title,
        markdown=f"# {title}\n\n{body}\n",
        extractor_used="trafilatura",
        fetched_at=FETCHED,
    )


class _Adapter:
    def __init__(self, results) -> None:
        self.id = "fake"
        self.results = results
        self.calls = 0

    async def search(self, query, **kwargs):
        self.calls += 1
        return list(self.results)


class _Gateway:
    provider = "openai"
    default_quick_model = "test-model"

    def __init__(self) -> None:
        self.calls = 0

    def with_override(self, override):
        return self

    async def stream(self, messages, **kwargs):
        self.calls += 1
        yield StreamDelta(text="Postgres uses btree indexes [1].", finish_reason="stop")
        yield StreamDelta(usage=Usage(input_tokens=5, output_tokens=5))


URL_A = "https://a.example/1"
URL_B = "https://docs.b.example/2"


def _install(results=None, pages=None):
    _local_cache.clear()
    adapter = _Adapter(
        results
        if results is not None
        else [
            _raw(URL_A, "PostgreSQL indexes", "postgres vacuum btree planner statistics"),
            _raw(URL_B, "Postgres tuning", "postgres btree planner statistics index"),
        ]
    )
    pages_map = pages if pages is not None else {URL_A: _page(URL_A, "A"), URL_B: _page(URL_B, "B")}
    store = InMemoryEngineStore(
        {"fake": EngineConfig(id="fake", name="fake", category="general", enabled=True)}
    )
    gateway = _Gateway()

    class Scraper:
        async def scrape_many(self, urls, **kwargs):
            return [
                pages_map.get(u) or PageFailure(url=u, status="failed", error="x") for u in urls
            ]

    class Embedder:
        enabled = False

    cache = CacheClient(None)
    deps.get_cache = lambda: cache
    deps.get_engine_store = lambda: store
    deps.build_adapters = lambda s: {"fake": adapter}
    deps.get_scraper = lambda: Scraper()
    deps.get_gateway = lambda: gateway
    deps.get_embedder = lambda: Embedder()
    deps.get_reranker = lambda: HeuristicReranker()
    deps.reset_pipelines()
    return adapter, gateway


@pytest.fixture(autouse=True)
async def setup_w2_d_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'd.db').as_posix()}")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("ENGINE_STORE_BACKEND", "memory")
    monkeypatch.setenv("SEARXNG_ENABLED", "false")
    get_settings.cache_clear()
    await dispose_engine()
    await ensure_schema()
    saved = {name: getattr(deps, name) for name in _DEPS_SWAPPED}
    yield
    for name, value in saved.items():
        setattr(deps, name, value)
    deps.reset_pipelines()
    _local_cache.clear()
    await dispose_engine()
    get_settings.cache_clear()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# ------------------------------------------------------------------ provenance marks


async def test_search_results_carry_marks_and_null_when_unknown():
    as_of = datetime.now(UTC) - timedelta(hours=2)
    _install(
        [
            _raw(
                URL_A,
                "VCB close",
                "postgres price",
                source_url="https://dchart.example/vcb",
                source_name="dchart",
                as_of=as_of,
                data_kind="observed",
            ),
            _raw(URL_B, "Plain page", "postgres page"),
        ]
    )
    async with _client() as c:
        body = (await c.post("/v1/search", json={"query": "postgres marks"})).json()
    by_url = {r["url"]: r for r in body["results"]}
    a, b = by_url[URL_A], by_url[URL_B]
    assert a["source_url"] == "https://dchart.example/vcb"
    assert a["data_kind"] == "observed"
    assert a["as_of"] == as_of.isoformat()
    assert 7000 <= a["age_s"] <= 7300
    for key in ("source_url", "data_kind", "as_of", "age_s", "published_at"):
        assert key in b and b[key] is None, key


async def test_marks_survive_the_search_cache():
    as_of = datetime(2026, 11, 7, tzinfo=UTC)
    adapter, _ = _install(
        [
            _raw(
                URL_A,
                "VCB",
                "postgres",
                source_url="https://s.example",
                as_of=as_of,
                data_kind="observed",
            )
        ]
    )
    async with _client() as c:
        await c.post("/v1/search", json={"query": "postgres cached marks"})
        second = (await c.post("/v1/search", json={"query": "postgres cached marks"})).json()
    assert second["cached"] is True and adapter.calls == 1
    first = second["results"][0]
    assert first["data_kind"] == "observed"
    assert first["source_url"] == "https://s.example"
    assert first["as_of"] == as_of.isoformat()


async def test_context_chunks_have_dates():
    _install()
    async with _client() as c:
        body = (await c.post("/v1/context", json={"query": "postgres btree", "k": 2})).json()
    assert body["chunks"]
    for ch in body["chunks"]:
        assert ch["fetched_at"] == FETCHED.isoformat()
        assert ch["source_url"] == ch["url"]
        for key in ("data_kind", "published_at", "as_of", "age_s"):
            assert key in ch
    for src in body["sources"]:
        assert "source_url" in src and "data_kind" in src


# ------------------------------------------------------------------ /v1/context include


async def test_context_default_is_chunks_and_sources_only():
    _install()
    async with _client() as c:
        small = await c.post("/v1/context", json={"query": "postgres default", "k": 2})
        full = await c.post(
            "/v1/context",
            json={
                "query": "postgres default",
                "k": 2,
                "include": ["chunks", "sources", "context_md", "messages", "system_prompt"],
            },
        )
    s, f = small.json(), full.json()
    assert "chunks" in s and "sources" in s
    for part in ("context_md", "messages", "system_prompt", "domain_intelligence"):
        assert part not in s, part
    assert "[1]" in f["context_md"] and f["messages"][0]["role"] == "system"
    assert "system_prompt" in f
    assert len(small.content) <= 0.5 * len(full.content)


async def test_context_include_rejects_unknown_part():
    _install()
    async with _client() as c:
        r = await c.post("/v1/context", json={"query": "postgres", "include": ["everything"]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


# ------------------------------------------------------------------ domain filters


async def test_include_and_exclude_domains_on_search():
    _install()
    async with _client() as c:
        inc = (
            await c.post("/v1/search", json={"query": "postgres", "include_domains": ["a.example"]})
        ).json()
        sub = (
            await c.post("/v1/search", json={"query": "postgres", "include_domains": ["b.example"]})
        ).json()
        exc = (
            await c.post(
                "/v1/search", json={"query": "postgres", "exclude_domains": ["www.A.example"]}
            )
        ).json()
    assert [r["url"] for r in inc["results"]] == [URL_A]
    assert [r["url"] for r in sub["results"]] == [URL_B], "a domain also matches its subdomains"
    assert [r["url"] for r in exc["results"]] == [URL_B]
    assert inc["complete"] is True


async def test_filter_that_removes_everything_is_not_a_true_empty():
    adapter, _ = _install()
    async with _client() as c:
        r = await c.post(
            "/v1/search", json={"query": "postgres", "include_domains": ["nowhere.example"]}
        )
        again = await c.post("/v1/search", json={"query": "postgres"})
    body = r.json()
    assert r.status_code == 200 and body["results"] == [] and body["complete"] is False
    assert len(again.json()["results"]) == 2, "the filter must not poison the shared cache"
    assert adapter.calls == 1, "filtered and unfiltered calls share one fan-out"


async def test_context_honours_domain_filters():
    _install()
    async with _client() as c:
        body = (
            await c.post(
                "/v1/context", json={"query": "postgres", "k": 2, "exclude_domains": ["a.example"]}
            )
        ).json()
    assert {s["url"] for s in body["sources"]} == {URL_B}
    assert {ch["url"] for ch in body["chunks"]} == {URL_B}


async def test_filtered_answer_is_neither_cached_nor_replayed():
    _, gateway = _install()
    async with _client() as c:
        await c.post(
            "/v1/answer", json={"query": "postgres answer", "include_domains": ["a.example"]}
        )
        await asyncio.sleep(0.05)
        plain = (await c.post("/v1/answer", json={"query": "postgres answer"})).json()
        await asyncio.sleep(0.05)
        plain_again = (await c.post("/v1/answer", json={"query": "postgres answer"})).json()
        filtered_again = (
            await c.post(
                "/v1/answer", json={"query": "postgres answer", "include_domains": ["a.example"]}
            )
        ).json()
    assert plain["cached"] is False, "an unfiltered caller must not get the filtered answer"
    assert plain_again["cached"] is True
    assert filtered_again["cached"] is False
    assert gateway.calls == 3


# ------------------------------------------------------------------ citations


async def test_citations_report_marker_valid():
    _install()
    async with _client() as c:
        ok = (
            await c.post(
                "/v1/citations/validate",
                json={"answer_md": "Claim [1].", "chunks": [{"url": URL_A, "content": "text"}]},
            )
        ).json()
        bad = (
            await c.post(
                "/v1/citations/validate",
                json={
                    "answer_md": "Claim [1]. Other [7].",
                    "chunks": [{"url": URL_A, "content": "t"}],
                },
            )
        ).json()
    assert ok["marker_valid"] is True and ok["citations"][0]["marker_valid"] is True
    assert bad["marker_valid"] is False and bad["invalid_markers"] == [7]


# ------------------------------------------------------------------ contract_check.py


def _load_contract_check():
    path = Path(__file__).resolve().parents[1] / "scripts" / "contract_check.py"
    spec = importlib.util.spec_from_file_location("contract_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return path, module


def test_contract_check_needs_an_explicit_base_url():
    path, module = _load_contract_check()
    with pytest.raises(SystemExit) as exc:
        module.main([])
    assert exc.value.code == 2
    source = path.read_text(encoding="utf-8")
    assert "8001" not in source, "no default may point at another system's port"
    assert "from app" not in source and "import app" not in source


def test_contract_check_fails_when_the_api_is_unreachable(capsys):
    _, module = _load_contract_check()
    code = module.main(["--base-url", "http://127.0.0.1:9", "--timeout", "0.5"])
    assert code == 1
    assert "FAIL" in capsys.readouterr().out
