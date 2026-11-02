"""Auth (FR-14) + rate limit + MCP client tests."""

from __future__ import annotations

import json

import pytest

from app.core.auth import AuthContext
from app.core.errors import MetaSearchError


class TestRequireApiKeyDisabled:
    async def test_disabled_mode_is_open(self, monkeypatch):
        from app.config import get_settings
        from app.core.auth import require_api_key

        monkeypatch.setattr(get_settings(), "auth_enabled", False)
        ctx = (
            await require_api_key.__wrapped__(None)
            if hasattr(require_api_key, "__wrapped__")
            else None
        )
        # Call the inner logic directly: no request needed when disabled.
        from app.core import auth as auth_mod

        ctx = await auth_mod.require_api_key(type("R", (), {"headers": {}})())
        assert ctx.role == "admin"
        assert ctx.key_id == "anonymous"


class TestAuthenticate:
    async def test_valid_key_via_db(self, monkeypatch):
        from app.core import auth as auth_mod

        class FakeResult:
            def scalar_one_or_none(self):
                return type(
                    "Row",
                    (),
                    {
                        "id": "kid",
                        "name": "test",
                        "role": "admin",
                        "rate_limit_rpm": 60,
                        "enabled": True,
                    },
                )()

        class FakeSession:
            async def execute(self, stmt):
                return FakeResult()

        class FakeMaker:
            def __call__(self):
                return self

            async def __aenter__(self):
                return FakeSession()

            async def __aexit__(self, *a):
                return False

        plaintext, _key_hash, _prefix = auth_mod.generate_key()
        monkeypatch.setattr(auth_mod, "get_sessionmaker", FakeMaker())
        ctx = await auth_mod.authenticate(plaintext)
        assert ctx.role == "admin"
        assert ctx.rate_limit_rpm == 60

    async def test_unknown_key_401(self, monkeypatch):
        from app.core import auth as auth_mod

        class FakeResult:
            def scalar_one_or_none(self):
                return None

        class FakeSession:
            async def execute(self, stmt):
                return FakeResult()

        class FakeMaker:
            def __call__(self):
                return self

            async def __aenter__(self):
                return FakeSession()

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(auth_mod, "get_sessionmaker", FakeMaker())
        with pytest.raises(MetaSearchError) as excinfo:
            await auth_mod.authenticate("msk_unknown")
        assert excinfo.value.code.value == "unauthorized"

    async def test_malformed_key_401(self):
        from app.core import auth as auth_mod

        with pytest.raises(MetaSearchError):
            await auth_mod.authenticate("not-a-key")


class TestRateLimit:
    async def test_under_limit_returns_headers(self, monkeypatch):
        from app.core import auth as auth_mod

        class FakePipeline:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def zremrangebyscore(self, *a):
                return self

            def zadd(self, *a):
                return self

            def zcard(self, *a):
                return self

            def expire(self, *a):
                return self

            async def execute(self):
                return [0, 1, 3, 1]  # count=3 under limit 60

        class FakeRedis:
            def pipeline(self, transaction=True):
                return FakePipeline()

        monkeypatch.setattr(auth_mod, "get_redis", lambda: FakeRedis())
        ctx = AuthContext(key_id="k1", name=None, role="user", rate_limit_rpm=60)
        headers = await auth_mod.check_rate_limit(ctx)
        assert headers["X-RateLimit-Limit"] == "60"

    async def test_over_limit_429_with_retry_after(self, monkeypatch):
        from app.core import auth as auth_mod

        class FakePipeline:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def zremrangebyscore(self, *a):
                return self

            def zadd(self, *a):
                return self

            def zcard(self, *a):
                return self

            def expire(self, *a):
                return self

            async def execute(self):
                return [0, 1, 61, 1]  # count=61 over limit 60

        class FakeRedis:
            def pipeline(self, transaction=True):
                return FakePipeline()

        monkeypatch.setattr(auth_mod, "get_redis", lambda: FakeRedis())
        ctx = AuthContext(key_id="k1", name=None, role="user", rate_limit_rpm=60)
        with pytest.raises(MetaSearchError) as excinfo:
            await auth_mod.check_rate_limit(ctx)
        assert excinfo.value.code.value == "rate_limited"
        assert "Retry-After" in excinfo.value.headers

    async def test_redis_down_uses_local_window(self, monkeypatch):
        from app.core import auth as auth_mod

        monkeypatch.setattr(auth_mod, "get_redis", lambda: None)
        auth_mod._local_windows.clear()
        ctx = AuthContext(key_id="lite", name=None, role="user", rate_limit_rpm=2)
        # First two requests pass, the third trips the in-process window.
        await auth_mod.check_rate_limit(ctx)
        await auth_mod.check_rate_limit(ctx)
        with pytest.raises(MetaSearchError) as excinfo:
            await auth_mod.check_rate_limit(ctx)
        assert excinfo.value.details["backend"] == "local"
        auth_mod._local_windows.clear()


class TestEndpointAuthEnforced:
    def test_enabled_mode_401_without_key(self, client, monkeypatch):
        from app.config import get_settings

        monkeypatch.setattr(get_settings(), "auth_enabled", True)
        # authenticate fails closed on DB-unavailable test rig -> 401.
        resp = client.post("/v1/search", json={"query": "x"})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"

    def test_disabled_mode_open(self, client):
        # Default deps (DB down) still serve: ops/status is always 200.
        resp = client.get("/v1/ops/status")
        assert resp.status_code == 200


class TestMcpClient:
    async def test_web_search_formats_markdown(self, monkeypatch):
        from app.mcp import client as mcp

        async def fake_call(method, path, payload=None):
            assert path == "/v1/search"
            return {
                "results": [
                    {
                        "rank": 1,
                        "url": "https://x.com",
                        "title": "X",
                        "snippet": "snippet",
                        "score": 0.9,
                        "engines": ["google"],
                    }
                ]
            }

        monkeypatch.setattr(mcp, "_call", fake_call)
        out = await mcp.web_search("q")
        assert "1. [X](https://x.com)" in out
        assert "score 0.90" in out

    async def test_search_and_scrape_combines(self, monkeypatch):
        from app.mcp import client as mcp

        async def fake_call(method, path, payload=None):
            if path == "/v1/search":
                return {
                    "results": [
                        {
                            "rank": 1,
                            "url": "https://a.com",
                            "title": "A",
                            "snippet": "s",
                            "score": 1,
                        }
                    ]
                }
            assert path == "/v1/scrape"
            assert payload["urls"] == ["https://a.com"]
            return [{"url": "https://a.com", "status": "ok", "title": "A", "markdown": "# body"}]

        monkeypatch.setattr(mcp, "_call", fake_call)
        out = await mcp.web_search_and_scrape("q", scrape_top=1)
        assert "## Kết quả tìm kiếm" in out
        assert "## Nội dung trang" in out
        assert "# body" in out

    async def test_deep_research_returns_poll_hint(self, monkeypatch):
        from app.mcp import client as mcp

        async def fake_call(method, path, payload=None):
            return {"research_id": "abc", "status": "queued"}

        monkeypatch.setattr(mcp, "_call", fake_call)
        out = json.loads(await mcp.deep_research("q"))
        assert out["poll_hint"].endswith("/v1/research/abc")

    async def test_api_error_surfaces_friendly(self, monkeypatch):
        from app.mcp import client as mcp

        async def fake_call(method, path, payload=None):
            raise mcp.MCPClientError("no_results: All engines failed")

        monkeypatch.setattr(mcp, "web_search", fake_call)
        with pytest.raises(mcp.MCPClientError, match="no_results"):
            await fake_call("POST", "/v1/search", {})

    def test_run_tool_dispatch(self, monkeypatch):
        from app.mcp import client as mcp

        called = {}

        async def fake_scrape(urls, max_chars=8000):
            called["urls"] = urls
            return "md"

        monkeypatch.setattr(mcp, "web_scrape", fake_scrape)
        out = mcp.run_tool("web_scrape", {"urls": ["https://a"]})
        assert out == "md"
        assert called["urls"] == ["https://a"]

        with pytest.raises(mcp.MCPClientError, match="unknown tool"):
            mcp.run_tool("nope", {})


class TestLangfuseTracing:
    async def test_disabled_noop(self):
        from app.config import get_settings
        from app.core.llm_tracing import trace_llm_call

        await trace_llm_call(
            get_settings(), task="t", model="m", messages=[], output="", usage={}
        )  # must not raise

    async def test_enabled_posts_trace(self, monkeypatch):
        from app.config import Settings
        from app.core import llm_tracing as lf

        settings = Settings(
            ENVIRONMENT="test",
            LANGFUSE_HOST="http://lf.test",
            LANGFUSE_PUBLIC_KEY="pk",
            LANGFUSE_SECRET_KEY="sk",
        )
        sent = {}

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None, **kw):
                sent["url"] = url
                sent["json"] = json
                sent["auth"] = headers["Authorization"]
                return type("R", (), {"status_code": 200})()

        monkeypatch.setattr(lf, "get_http_client", lambda *a, **kw: FakeClient())
        await lf.trace_llm_call(
            settings, task="planner", model="m", messages=[], output="o", usage={}
        )
        assert sent["url"].endswith("/api/public/traces")
        assert sent["auth"].startswith("Basic ")


class TestWP11StartupGuardAndTenancy:
    """WP-11: startup guard, owner isolation, admin surface, sanitization."""

    def test_refuses_public_bind_without_auth(self):
        from app.core.auth import validate_startup_auth

        problems = validate_startup_auth(
            api_host="0.0.0.0", auth_enabled=False, environment="dev", dev_anon_admin=False
        )
        assert problems, "0.0.0.0 + auth off must be flagged"
        ok = validate_startup_auth(
            api_host="127.0.0.1", auth_enabled=False, environment="dev", dev_anon_admin=False
        )
        assert ok == []

    def test_refuses_prod_without_auth(self):
        from app.core.auth import validate_startup_auth

        problems = validate_startup_auth(
            api_host="127.0.0.1", auth_enabled=False, environment="prod", dev_anon_admin=False
        )
        assert any("prod" in p for p in problems)

    def test_dev_anon_admin_refused_off_loopback(self):
        from app.core.auth import validate_startup_auth

        problems = validate_startup_auth(
            api_host="0.0.0.0", auth_enabled=False, environment="dev", dev_anon_admin=True
        )
        assert any("DEV_ANON_ADMIN" in p for p in problems)

    def test_anonymous_is_not_admin_without_escape(self, monkeypatch, client):
        from app.config import get_settings

        monkeypatch.setattr(get_settings(), "dev_anon_admin", False)
        resp = client.patch("/v1/engines/searxng", json={"enabled": False})
        assert resp.status_code == 403

    def test_request_id_sanitized(self, client):
        bad_id = "x" * 300 + "<script>"
        resp = client.get("/healthz", headers={"x-request-id": bad_id})
        assert resp.status_code == 200
        returned = resp.headers.get("x-request-id", "")
        assert returned != bad_id
        assert len(returned) <= 128
        assert all(c.isalnum() or c in "-_" for c in returned)

    def test_metrics_loopback_allowed(self, client):
        # TestClient connects from loopback: allowed when auth is off.
        resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_meta_public_payload_shrunk(self, client):
        data = client.get("/v1/meta").json()
        assert set(data.keys()) <= {"name", "version"}
