"""Tests for Wave 2 Package A: Auth Context Isolation and Surface Protection."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings, get_settings
from app.core.auth import (
    authenticate,
    create_key,
    reset_rate_limit_windows,
    validate_startup_auth,
)
from app.core.errors import ErrorCode, MetaSearchError
from app.db.bootstrap import ensure_schema
from app.db.models import Query
from app.db.session import get_sessionmaker
from app.llm.providers import (
    LLMOverride,
    resolve_llm_target,
)
from app.retrieval.models import EngineConfig, RawResult


def _hdr(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _raw(url: str, title: str = "T", snippet: str = "S") -> RawResult:
    return RawResult(
        url=url, title=title, snippet=snippet, adapter="fake", source_engines=[], rank=0
    )


class _FakeAdapter:
    def __init__(self, engine_id: str, results=None):
        self.id = engine_id
        self.results = results or []

    async def search(self, query, **kwargs):
        return list(self.results)


class _FakeGateway:
    provider = "openai"
    default_quick_model = "m"

    def with_override(self, override):
        return self

    async def stream(self, messages, **kwargs):
        from app.llm.gateway import StreamDelta, Usage

        yield StreamDelta(text="Answer text [1].")
        yield StreamDelta(usage=Usage(input_tokens=5, output_tokens=5))


def _setup_stack(adapters, app_instance=None):
    from app.api import deps
    from app.cache import CacheClient, _local_cache
    from app.extraction.scraper import PageFailure
    from app.orchestration.engine_store import InMemoryEngineStore
    from app.retrieval.reranker import HeuristicReranker

    class Scraper:
        async def scrape_many(self, urls, **kwargs):
            return [PageFailure(url=u, status="failed", error="not found") for u in urls]

    class Embedder:
        enabled = False

        async def embed(self, text):
            from app.llm.gateway import LLMError

            raise LLMError("disabled")

    _local_cache.clear()
    configs = {
        a.id: EngineConfig(id=a.id, name=a.id, category="general", enabled=True) for a in adapters
    }
    store = InMemoryEngineStore(configs)
    cache = CacheClient(None)
    gw = _FakeGateway()
    deps.get_engine_store = lambda: store
    deps.build_adapters = lambda s: {a.id: a for a in adapters}
    deps.get_cache = lambda: cache
    deps.get_scraper = lambda: Scraper()
    deps.get_gateway = lambda: gw
    deps.get_embedder = lambda: Embedder()
    deps.get_reranker = lambda: HeuristicReranker()
    deps.reset_pipelines()
    return gw


@pytest.fixture
async def isolation_db(tmp_path, monkeypatch):
    db_file = tmp_path / "iso.db"
    db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("ENGINE_STORE_BACKEND", "memory")
    monkeypatch.setenv("SEARXNG_ENABLED", "false")
    monkeypatch.setenv("DEV_ANON_ADMIN", "false")
    get_settings.cache_clear()
    from app.db.session import dispose_engine

    await dispose_engine()
    await ensure_schema()
    reset_rate_limit_windows()
    _setup_stack([_FakeAdapter("fake", [_raw("https://ex.com/1")])])
    yield db_file


@pytest.mark.asyncio
async def test_expired_key_rejected(isolation_db):
    """Bắt buộc: Khóa đã hết hạn phải bị từ chối 401 ở tầng DB query authenticate().

    Khi gỡ bỏ kiểm tra `(ApiKeyRow.expires_at.is_(None)) | (ApiKeyRow.expires_at > now_utc)`,
    test này phải đỏ ngay lập tức!
    """
    expired_time = datetime.now(UTC) - timedelta(days=2)
    key_plain, _ = await create_key(
        "expired-tester",
        role="user",
        rate_limit_rpm=60,
        expires_at=expired_time,
    )
    with pytest.raises(MetaSearchError) as exc_info:
        await authenticate(key_plain)
    assert exc_info.value.code == ErrorCode.UNAUTHORIZED
    assert exc_info.value.status_code == 401


@pytest.mark.parametrize("route_type", ["list", "detail"])
@pytest.mark.asyncio
async def test_key_a_cannot_read_key_b_query(isolation_db, route_type):
    """Bắt buộc: Key B không đọc được truy vấn của Key A (404 trên detail, ẩn trên list)."""
    from app.main import app

    key_a, _ = await create_key("user_a", rate_limit_rpm=6000)
    key_b, _ = await create_key("user_b", rate_limit_rpm=6000)
    root, _ = await create_key("admin_user", role="admin", rate_limit_rpm=6000)

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False, client=("127.0.0.1", 4242))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # A creates a query
        r = await client.post("/v1/search", json={"query": "SECRET-A-QUERY"}, headers=_hdr(key_a))
        assert r.status_code == 200

        # Wait for persistence
        async def find_query():
            async with get_sessionmaker()() as s:
                return (
                    (await s.execute(select(Query).where(Query.raw_text == "SECRET-A-QUERY")))
                    .scalars()
                    .first()
                )

        q = None
        for _ in range(50):
            q = await find_query()
            if q is not None:
                break
            await asyncio.sleep(0.05)
        assert q is not None
        assert q.api_key_id not in (None, "anonymous")
        qid = str(q.id)

        if route_type == "list":
            # List as A shows
            r_a = await client.get("/v1/queries", headers=_hdr(key_a))
            assert "SECRET-A-QUERY" in r_a.text

            # List as B hides
            r_b = await client.get("/v1/queries", headers=_hdr(key_b))
            assert "SECRET-A-QUERY" not in r_b.text

            # List as admin shows
            r_admin = await client.get("/v1/queries", headers=_hdr(root))
            assert "SECRET-A-QUERY" in r_admin.text
        else:
            # Detail as A returns 200
            r_a = await client.get(f"/v1/queries/{qid}", headers=_hdr(key_a))
            assert r_a.status_code == 200

            # Detail as B returns 404
            r_b = await client.get(f"/v1/queries/{qid}", headers=_hdr(key_b))
            assert r_b.status_code == 404
            assert "SECRET-A-QUERY" not in r_b.text


@pytest.mark.asyncio
async def test_research_events_owner_only(isolation_db):
    """Bắt buộc: Khóa khác bị 404 trên cả /research/{id} và /events trước khi stream."""
    from app.main import app

    key_a, _ = await create_key("user_a", rate_limit_rpm=6000)
    key_b, _ = await create_key("user_b", rate_limit_rpm=6000)

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False, client=("127.0.0.1", 4242))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/research", json={"query": "SECRET-RESEARCH-QUERY"}, headers=_hdr(key_a)
        )
        assert r.status_code == 202
        rid = r.json()["research_id"]

        # Owner A can read
        r_a = await client.get(f"/v1/research/{rid}", headers=_hdr(key_a))
        assert r_a.status_code == 200

        # Other key B gets 404 on get
        r_b = await client.get(f"/v1/research/{rid}", headers=_hdr(key_b))
        assert r_b.status_code == 404
        assert "SECRET-RESEARCH" not in r_b.text

        # Other key B gets 404 on events (checked before EventSourceResponse)
        r_b_ev = await client.get(f"/v1/research/{rid}/events", headers=_hdr(key_b))
        assert r_b_ev.status_code == 404


@pytest.mark.asyncio
async def test_metrics_requires_admin_when_exposed(isolation_db):
    """Bắt buộc: /metrics yêu cầu admin thực khi gọi từ xa, loopback cho phép khi no-auth."""
    from app.main import app

    user_key, _ = await create_key("u_norm", role="user", rate_limit_rpm=6000)
    admin_key, _ = await create_key("u_admin", role="admin", rate_limit_rpm=6000)

    # Remote client
    remote_transport = httpx.ASGITransport(
        app=app, raise_app_exceptions=False, client=("203.0.113.10", 5555)
    )
    async with httpx.AsyncClient(transport=remote_transport, base_url="http://test") as client:
        # Anonymous -> 403
        r_anon = await client.get("/metrics")
        assert r_anon.status_code == 403

        # User role -> 403
        r_user = await client.get("/metrics", headers=_hdr(user_key))
        assert r_user.status_code == 403

        # Admin role -> 200
        r_admin = await client.get("/metrics", headers=_hdr(admin_key))
        assert r_admin.status_code == 200
        assert "metasearch" in r_admin.text


@pytest.mark.asyncio
async def test_admin_not_double_counted(isolation_db):
    """Bắt buộc: require_admin không đếm rate limit 2 lần khi tái sử dụng AuthContext."""
    from app.main import app

    admin_key, _ = await create_key("adm", role="admin", rate_limit_rpm=6000)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False, client=("127.0.0.1", 4242))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r1 = await client.patch("/v1/engines/fake", json={"weight": 1.0}, headers=_hdr(admin_key))
        r2 = await client.patch("/v1/engines/fake", json={"weight": 1.0}, headers=_hdr(admin_key))
        rem1 = int(r1.headers["x-ratelimit-remaining"])
        rem2 = int(r2.headers["x-ratelimit-remaining"])
        assert rem1 - rem2 == 1


def test_refuses_public_bind_without_auth():
    """Bắt buộc: Guard khởi động từ chối bind 0.0.0.0 nếu auth tắt."""
    probs_bad = validate_startup_auth(
        api_host="0.0.0.0",
        auth_enabled=False,
        environment="dev",
        dev_anon_admin=False,
    )
    assert len(probs_bad) > 0
    assert any("exposes the service" in p for p in probs_bad)

    probs_good = validate_startup_auth(
        api_host="127.0.0.1",
        auth_enabled=False,
        environment="dev",
        dev_anon_admin=False,
    )
    assert len(probs_good) == 0


def test_custom_headers_only_for_custom_provider():
    """Bắt buộc: CUSTOM_LLM_HEADERS chỉ gắn khi provider là custom và base_url từ env."""
    s = Settings(
        llm_provider="custom",
        custom_llm_base_url="https://llm.internal.test/v1",
        custom_llm_api_key="secret",
        custom_llm_headers='{"X-Internal-Token": "secret-hdr-123"}',
        groq_api_key="groq-secret",
    )
    # Not custom provider -> header stripped
    t_groq = resolve_llm_target(s, request_override=LLMOverride(provider="groq"))
    assert "X-Internal-Token" not in (t_groq.extra_headers or {})

    # Custom provider with env base_url -> header attached
    t_custom = resolve_llm_target(s)
    assert "X-Internal-Token" in (t_custom.extra_headers or {})


def test_byo_empty_allowlist_rejects():
    """Bắt buộc: BYO bật nhưng allowlist rỗng phải từ chối mọi host được yêu cầu."""
    s = Settings(llm_byo_enabled=True, llm_byo_allowed_hosts="")
    with pytest.raises(MetaSearchError) as exc_info:
        resolve_llm_target(s, request_override=LLMOverride(base_url="https://attacker.example/v1"))
    assert exc_info.value.code == ErrorCode.INVALID_REQUEST


def test_runtime_key_not_sent_to_different_provider():
    """Bắt buộc: Khóa runtime của provider này không được gửi sang provider khác."""
    s = Settings(llm_provider="openai", groq_api_key="env-groq-key")
    t = resolve_llm_target(
        s,
        runtime_override=LLMOverride(provider="openrouter", api_key="RUNTIME-OR-KEY"),
        request_override=LLMOverride(provider="groq"),
    )
    assert t.api_key != "RUNTIME-OR-KEY"
    assert t.api_key == "env-groq-key"


@pytest.mark.asyncio
async def test_surface_protection_admin_endpoints(isolation_db):
    """Kiểm tra các endpoint bề mặt (/meta/full, /llm/config, /docs...)."""
    from app.main import app

    user_key, _ = await create_key("u_test", role="user", rate_limit_rpm=6000)
    admin_key, _ = await create_key("a_test", role="admin", rate_limit_rpm=6000)

    # Remote client
    remote_transport = httpx.ASGITransport(
        app=app, raise_app_exceptions=False, client=("198.51.100.2", 8888)
    )
    async with httpx.AsyncClient(transport=remote_transport, base_url="http://test") as client:
        for path in ("/v1/meta/full", "/v1/llm/config", "/v1/llm/dream-rsi/state"):
            # Anonymous -> 401
            r_anon = await client.get(path)
            assert r_anon.status_code in (401, 403)
            # User -> 403
            r_user = await client.get(path, headers=_hdr(user_key))
            assert r_user.status_code == 403
            # Admin -> 200
            r_admin = await client.get(path, headers=_hdr(admin_key))
            assert r_admin.status_code == 200

        # Remote /docs and /openapi.json
        r_docs_anon = await client.get("/docs")
        assert r_docs_anon.status_code == 403
        r_open_anon = await client.get("/openapi.json")
        assert r_open_anon.status_code == 403

        r_docs_user = await client.get("/docs", headers=_hdr(user_key))
        assert r_docs_user.status_code == 403

        r_docs_admin = await client.get("/docs", headers=_hdr(admin_key))
        assert r_docs_admin.status_code == 200
        r_open_admin = await client.get("/openapi.json", headers=_hdr(admin_key))
        assert r_open_admin.status_code == 200

    # Loopback client
    loopback_transport = httpx.ASGITransport(
        app=app, raise_app_exceptions=False, client=("127.0.0.1", 8888)
    )
    async with httpx.AsyncClient(transport=loopback_transport, base_url="http://test") as client:
        r_docs_loop = await client.get("/docs")
        assert r_docs_loop.status_code == 200
        r_open_loop = await client.get("/openapi.json")
        assert r_open_loop.status_code == 200
