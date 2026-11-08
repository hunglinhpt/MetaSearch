"""Wave 3 package D: LLM pool and timeouts, 503 when every provider cools down, honest
scraper identity, CPU work off the event loop, and a harness that measures only its own server."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime

import httpx
import pytest

import app.extraction.scraper as scraper_mod
import app.llm.gateway as gw_mod
import app.orchestration.pipeline as pipeline_mod
from app.api import deps
from app.config import get_settings
from app.db.bootstrap import ensure_schema
from app.db.session import dispose_engine
from app.extraction.scraper import PageContent, PageFailure
from app.llm.gateway import LLMGateway, llm_breakers
from app.main import app
from app.orchestration.engine_store import InMemoryEngineStore
from app.retrieval.health import Outcome
from app.retrieval.models import EngineConfig, RawResult
from app.retrieval.reranker import HeuristicReranker

URL = "https://a.example/1"
_SWAPPED = (
    "get_engine_store",
    "build_adapters",
    "get_scraper",
    "get_gateway",
    "get_embedder",
    "get_reranker",
)


def _sse(request: httpx.Request) -> httpx.Response:
    body = 'data: {"choices":[{"delta":{"content":"Postgres [1]."}}]}\n\ndata: [DONE]\n\n'
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


class _Adapter:
    id = "fake"

    async def search(self, query, **kwargs):
        return [
            RawResult(
                url=URL,
                title="Postgres",
                snippet="postgres btree",
                adapter="fake",
                source_engines=[],
                rank=0,
            )
        ]


def _install(gateway=None) -> None:
    store = InMemoryEngineStore(
        {"fake": EngineConfig(id="fake", name="fake", category="general", enabled=True)}
    )
    page = PageContent(
        url=URL,
        final_url=URL,
        title="T",
        markdown="# T\n\n" + "postgres btree " * 80,
        extractor_used="trafilatura",
        fetched_at=datetime.now(UTC),
    )

    class Scraper:
        async def scrape_many(self, urls, **kwargs):
            return [
                page if u == URL else PageFailure(url=u, status="failed", error="x") for u in urls
            ]

    class Embedder:
        enabled = False

    gw = gateway or LLMGateway(
        get_settings(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(_sse)),
        use_runtime_store=False,
    )
    deps.get_engine_store = lambda: store
    deps.build_adapters = lambda s: {"fake": _Adapter()}
    deps.get_scraper = lambda: Scraper()
    deps.get_gateway = lambda: gw
    deps.get_embedder = lambda: Embedder()
    deps.get_reranker = lambda: HeuristicReranker()
    deps.reset_pipelines()


@pytest.fixture(autouse=True)
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'd.db').as_posix()}")
    monkeypatch.setenv("ENGINE_STORE_BACKEND", "memory")
    monkeypatch.setenv("SEARXNG_ENABLED", "false")
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.setenv("CUSTOM_LLM_BASE_URL", "https://llm.test.invalid/v1")
    monkeypatch.setenv("CUSTOM_LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_API_KEY", "k")
    get_settings.cache_clear()
    await dispose_engine()
    await ensure_schema()
    llm_breakers._breakers.clear()
    saved = {n: getattr(deps, n) for n in _SWAPPED}
    yield
    for n, v in saved.items():
        setattr(deps, n, v)
    deps.reset_pipelines()
    llm_breakers._breakers.clear()
    await dispose_engine()
    get_settings.cache_clear()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=30
    )


async def _drain(gw) -> None:
    async for _ in gw.stream([{"role": "user", "content": "hi"}]):
        pass


async def test_gateway_uses_llm_pool_without_client(monkeypatch):
    pools = []

    def recorder(pool="internal"):
        pools.append(pool)
        return httpx.AsyncClient(transport=httpx.MockTransport(_sse))

    monkeypatch.setattr(gw_mod, "get_http_client", recorder)
    await _drain(LLMGateway(get_settings(), use_runtime_store=False))
    assert pools == ["llm"]


async def test_gateway_keeps_pool_timeout():
    seen = {}

    def handler(request):
        seen.update(request.extensions["timeout"])
        return _sse(request)

    await _drain(
        LLMGateway(
            get_settings(),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            use_runtime_store=False,
        )
    )
    assert seen["pool"] == 1.0
    assert seen["read"] == get_settings().request_timeout_s


async def test_llm_breaker_open_is_503():
    _install()
    breaker = llm_breakers.get("llm:custom")
    for _ in range(3):
        breaker.record(Outcome.UPSTREAM_ERROR)
    async with _client() as c:
        r = await c.post("/v1/answer", json={"query": f"postgres cool {uuid.uuid4().hex}"})
        s = await c.post(
            "/v1/answer", json={"query": f"postgres cool sse {uuid.uuid4().hex}", "stream": True}
        )
    assert r.status_code == 503, r.text
    assert r.json()["error"]["code"] == "overloaded"
    assert int(r.headers["retry-after"]) >= 1
    assert "event: error" in s.text and '"code": "overloaded"' in s.text


async def test_llm_failure_is_still_internal():
    _install(
        LLMGateway(
            get_settings(),
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(401, json={}))
            ),
            use_runtime_store=False,
        )
    )
    async with _client() as c:
        r = await c.post("/v1/answer", json={"query": f"postgres 401 {uuid.uuid4().hex}"})
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "internal"


def _real_scraper(handler):
    from app.cache import CacheClient

    s = scraper_mod.Scraper(
        get_settings(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        cache=CacheClient(None),
    )

    async def allowed(*a, **k):
        return True

    s._robots.allowed = allowed
    return s


async def _resolved(url):
    return frozenset({"93.184.216.34"})


def _html(request):
    return httpx.Response(
        200,
        text="<html><body>" + "<p>postgres text body</p>" * 80 + "</body></html>",
        headers={"content-type": "text/html"},
    )


async def test_scraper_identifies_itself(monkeypatch):
    import app.extraction.ssrf as ssrf

    monkeypatch.setattr(ssrf, "resolve_and_check", _resolved)
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return _html(request)

    page = await _real_scraper(handler).scrape(f"https://who.example/{uuid.uuid4().hex}")
    assert isinstance(page, PageContent)
    assert seen["user-agent"] == get_settings().user_agent
    assert not any(k.startswith("sec-fetch") for k in seen)


async def test_scrape_extract_off_loop(monkeypatch):
    import app.extraction.ssrf as ssrf

    monkeypatch.setattr(ssrf, "resolve_and_check", _resolved)
    threads = []
    real = scraper_mod.extract_static

    def spy(*args, **kwargs):
        threads.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(scraper_mod, "extract_static", spy)
    page = await _real_scraper(_html).scrape(f"https://off.example/{uuid.uuid4().hex}")
    assert isinstance(page, PageContent)
    assert threads and threading.get_ident() not in threads


async def test_score_chunks_off_loop(monkeypatch):
    threads = []
    real = pipeline_mod._score_chunks_sync

    def spy(*args, **kwargs):
        threads.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(pipeline_mod, "_score_chunks_sync", spy)
    _install()
    async with _client() as c:
        r = await c.post("/v1/context", json={"query": f"postgres score {uuid.uuid4().hex}"})
    assert r.status_code == 200, r.text
    assert threads and threading.get_ident() not in threads


async def test_ssl_context_built_at_startup():
    import app.http as http_mod

    http_mod._ssl_context = None
    async with app.router.lifespan_context(app):
        assert http_mod._ssl_context is not None


async def test_load_refuses_foreign_server(tmp_path, monkeypatch):
    import tempfile

    from scripts.perf import load

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    (tmp_path / "metasearch_bench_18999.token").write_text("right-token", encoding="ascii")

    def server(token):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"rss_mb": 1.0, "token": token})
            )
        )

    async with server("other-token") as c:
        with pytest.raises(RuntimeError, match="token"):
            await load._check_token(c, "http://127.0.0.1:18999")
    async with server(None) as c:
        with pytest.raises(RuntimeError):
            await load._check_token(c, "http://127.0.0.1:18999")
    async with server("right-token") as c:
        await load._check_token(c, "http://127.0.0.1:18999")


def test_missing_truststore_warns(monkeypatch):
    import builtins

    import app.http as http_mod

    real_import = builtins.__import__

    def no_truststore(name, *args, **kwargs):
        if name == "truststore":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    warnings = []
    monkeypatch.setattr(builtins, "__import__", no_truststore)
    monkeypatch.setattr(http_mod.logger, "warning", lambda event, **kw: warnings.append(event))
    monkeypatch.setenv("TLS_TRUST_STORE", "system")
    get_settings.cache_clear()
    monkeypatch.setattr(http_mod, "_ssl_context", None)
    ctx = http_mod.get_ssl_context()
    assert ctx is not None and ctx.check_hostname
    assert warnings == ["tls_trust_store_unavailable"]
