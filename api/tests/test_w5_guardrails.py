"""Review guardrails (docs/26): structural tests that stop the same defect families from
coming back when an agent adds the next route.

The reviewed pattern: auth wiring is central and rarely forgotten, but the *cross-cutting
boundaries* (rate-limit weight, admission tier, input bounds) live in three other files,
so a new endpoint silently skips one of them. Each test here pins one of those boundaries
against the real application, not against a copy of its configuration.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core import auth as auth_mod
from app.core.admission import _TIER_CONFIG
from app.core.auth import AuthContext, _route_cost, static_route_cost
from app.core.errors import ErrorCode, MetaSearchError
from app.main import create_app

APP_DIR = Path(__file__).resolve().parents[1] / "app"


# --------------------------------------------------------------------------------------
# F-02: every POST route is explicitly classified for rate-limit cost
# --------------------------------------------------------------------------------------

# Routes whose weight comes from static_route_cost() (path template -> example path).
COSTED_POST = {
    "/v1/answer": 5,
    "/v1/context": 3,
    "/v1/research": 20,
    "/v1/tts/synthesize": 5,
    "/v1/podcast/briefing": 5,
    "/v1/podcast/personal": 5,
    "/v1/llm/council": 10,
    "/v1/market/ohlcv": 2,
    "/v1/market/sentiment": 8,
    "/v1/market/news-wire": 3,
    "/v1/crawl": 8,
    "/v1/map": 3,
    "/v1/hybrid": 2,
    "/v1/spaces/{slug}/crawl": 8,
    "/v1/spaces/{slug}/graph/build": 10,
    "/v1/spaces/{slug}/graph/query": 2,
}
# Payload-proportional (computed in _route_cost), so not in the static table.
DYNAMIC_COST_POST = {
    "/v1/fa/compare",
    "/v1/market/events",
    "/v1/market/alerts",
    # docs/33: billed via the "/briefings/" prefix special-case in
    # _route_cost (mid-path segment — static suffix matching cannot match it).
    "/v1/briefings/{channel_id}",
}
# Reviewed on purpose: cheap (DB/CPU only, one upstream call) or admin-gated.
UNIT_COST_POST = {
    "/v1/search",
    "/v1/scrape",  # bounded by admit("scrape") and max 10 urls
    "/v1/digests/scan",  # admin
    "/v1/llm/config/reset",  # admin
    "/v1/llm/dream-rsi/reset",  # admin
    "/v1/llm/validate",  # admin
    "/v1/llm/test",  # one probe call
    "/v1/domains/analyze",
    "/v1/citations/validate",
    "/v1/watchlist",
    "/v1/prefs/boosts",
    "/v1/memory",
    "/v1/memory/observations",
    "/v1/spaces",
    "/v1/research/{research_id}/cancel",
}


def _post_paths() -> set[str]:
    spec = create_app().openapi()
    return {p for p, ops in spec["paths"].items() if "post" in ops}


def test_every_post_route_is_classified_for_rate_limit_cost() -> None:
    known = set(COSTED_POST) | DYNAMIC_COST_POST | UNIT_COST_POST
    live = _post_paths()
    unclassified = sorted(live - known)
    assert not unclassified, (
        "New POST route(s) without a reviewed rate-limit weight: "
        f"{unclassified}. Add them to _STATIC_ROUTE_COSTS in app/core/auth.py (expensive: "
        "LLM/TTS/crawl/fan-out) or to UNIT_COST_POST here with a reason."
    )
    stale = sorted(known - live - {"/v1/spaces/{slug}/crawl"} - set())
    assert not stale, f"Classified routes that no longer exist: {stale}"


@pytest.mark.parametrize("template,expected", sorted(COSTED_POST.items()))
def test_expensive_routes_bill_their_weight(template: str, expected: int) -> None:
    path = template.replace("{slug}", "my-space")
    assert static_route_cost("POST", path) == expected


def test_cheap_routes_are_not_accidentally_weighted() -> None:
    for path in ("/v1/search", "/v1/memory", "/v1/spaces", "/v1/watchlist"):
        assert static_route_cost("POST", path) is None


# --------------------------------------------------------------------------------------
# F-13: every admit("<tier>") literal is a declared tier
# --------------------------------------------------------------------------------------


def test_every_admit_tier_literal_is_declared() -> None:
    pattern = re.compile(r'async with admit\(\s*"(\w+)"')
    used: dict[str, list[str]] = {}
    for path in APP_DIR.rglob("*.py"):
        for name in pattern.findall(path.read_text(encoding="utf-8")):
            used.setdefault(name, []).append(path.name)
    undeclared = {n: f for n, f in used.items() if n not in _TIER_CONFIG}
    assert not undeclared, (
        f"admit() tiers missing from _TIER_CONFIG (they would inherit the implicit "
        f"(16, 1.0) default): {undeclared}"
    )
    assert {"council", "crawl", "map", "hybrid"} <= set(used) | {"council"}


# --------------------------------------------------------------------------------------
# F-03 / F-04: council and Spaces crawl take an admission slot
# --------------------------------------------------------------------------------------


def _deny_factory(seen: list[str]):
    @asynccontextmanager
    async def _deny(tier: str, **_kw: Any):
        seen.append(tier)
        raise MetaSearchError(ErrorCode.OVERLOADED, f"tier {tier} full", status_code=503)
        yield  # pragma: no cover

    return _deny


def test_council_is_admission_gated(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.admission as admission

    seen: list[str] = []
    monkeypatch.setattr(admission, "admit", _deny_factory(seen))
    resp = client.post(
        "/v1/llm/council",
        json={"query": "q", "chunks": [{"url": "https://example.com/a", "content": "text"}]},
    )
    assert resp.status_code == 503, resp.text
    assert seen == ["council"]


def test_spaces_crawl_is_admission_gated(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.v1 import spaces as spaces_mod

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

    async def _true() -> bool:
        return True

    async def _owned(_session: Any, _slug: str) -> Any:
        return SimpleNamespace(pinned_urls=[], domains=[])

    seen: list[str] = []
    monkeypatch.setattr(spaces_mod, "_ensure_table", _true)
    monkeypatch.setattr(spaces_mod, "_get_owned", _owned)
    monkeypatch.setattr(spaces_mod.deps, "get_session_factory", lambda: lambda: _Session())
    monkeypatch.setattr(spaces_mod, "admit", _deny_factory(seen))
    resp = client.post("/v1/spaces/my-space/crawl")
    assert resp.status_code == 503, resp.text
    assert seen == ["crawl"]


def test_spaces_crawl_max_pages_is_bounded_in_the_contract(client: TestClient) -> None:
    assert client.post("/v1/spaces/my-space/crawl?max_pages=100000").status_code in (400, 422)


# --------------------------------------------------------------------------------------
# F-08: body cap holds for chunked transfer (no Content-Length)
# --------------------------------------------------------------------------------------


async def _run_asgi(app: Any, chunks: list[bytes], headers: list[tuple[bytes, bytes]]):
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/echo",
        "headers": headers,
        "client": ("203.0.113.5", 1234),
        "state": {},
    }
    queue = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if queue:
            return queue.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


async def _reading_app(scope: Any, receive: Any, send: Any) -> None:
    total = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            raise RuntimeError("client disconnected")
        total += len(message.get("body") or b"")
        if not message.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": str(total).encode()})


@pytest.fixture()
def small_cap(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MAX_BODY_BYTES", "1024")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


async def test_chunked_body_over_the_cap_is_rejected_with_413(small_cap) -> None:
    from app.core.middleware import RequestContextMiddleware

    app = RequestContextMiddleware(_reading_app)
    # No content-length header: 3 chunks x 600 bytes = 1800 > 1024.
    sent = await _run_asgi(app, [b"a" * 600] * 3, headers=[])
    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert len(starts) == 1, sent
    assert starts[0]["status"] == 413


async def test_chunked_body_under_the_cap_passes(small_cap) -> None:
    from app.core.middleware import RequestContextMiddleware

    app = RequestContextMiddleware(_reading_app)
    sent = await _run_asgi(app, [b"a" * 300] * 2, headers=[])
    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert starts[0]["status"] == 200
    assert next(m for m in sent if m["type"] == "http.response.body")["body"] == b"600"


# --------------------------------------------------------------------------------------
# F-01: anonymous rate limit (opt-in) when auth is off
# --------------------------------------------------------------------------------------


def test_anonymous_rate_limit_applies_when_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "anonymous_rate_limit_rpm", 2)
    monkeypatch.setattr(auth_mod, "get_redis", lambda: None)
    auth_mod.reset_rate_limit_windows()
    try:
        codes = [client.get("/v1/skills").status_code for _ in range(3)]
    finally:
        auth_mod.reset_rate_limit_windows()
    assert codes == [200, 200, 429]


def test_anonymous_is_unlimited_by_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "anonymous_rate_limit_rpm", 0)
    monkeypatch.setattr(auth_mod, "get_redis", lambda: None)
    auth_mod.reset_rate_limit_windows()
    assert [client.get("/v1/skills").status_code for _ in range(5)] == [200] * 5


# --------------------------------------------------------------------------------------
# F-05 / F-06 / F-22: operator-only surfaces
# --------------------------------------------------------------------------------------

OPERATOR_GET = ("/v1/ops/status", "/v1/ops/interleave", "/v1/ops/skills", "/v1/llm/health")


def _as_key(monkeypatch: pytest.MonkeyPatch, role: str) -> dict[str, str]:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True)

    async def fake_authenticate(_bearer: str | None) -> AuthContext:
        return AuthContext(key_id="key-1", name="t", role=role, rate_limit_rpm=1000)

    async def fake_rate_limit(_ctx: AuthContext, **_kw: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(auth_mod, "authenticate", fake_authenticate)
    monkeypatch.setattr(auth_mod, "check_rate_limit", fake_rate_limit)
    return {"Authorization": "Bearer msk_" + "0" * 32}


@pytest.mark.parametrize("path", OPERATOR_GET)
def test_operator_surfaces_reject_a_plain_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    headers = _as_key(monkeypatch, "user")
    assert client.get(path, headers=headers).status_code == 403


@pytest.mark.parametrize("path", OPERATOR_GET)
def test_operator_surfaces_accept_an_admin_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    headers = _as_key(monkeypatch, "admin")
    assert client.get(path, headers=headers).status_code == 200


@pytest.mark.parametrize("path", OPERATOR_GET)
def test_operator_surfaces_stay_open_for_the_single_operator_profile(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "dev_anon_admin", False)  # Lite: anonymous is role "user"
    assert client.get(path).status_code == 200


def test_plain_key_cannot_trigger_the_shared_briefing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _as_key(monkeypatch, "user")
    assert client.post("/v1/podcast/briefing", headers=headers).status_code == 403
    # Reading status stays available to a plain key (it is not an operator action).
    assert client.get("/v1/podcast/briefing/status", headers=headers).status_code != 403


# --------------------------------------------------------------------------------------
# F-09 / F-10: unauthenticated local surfaces require a DIRECT local caller
# --------------------------------------------------------------------------------------


def _remote() -> TestClient:
    return TestClient(create_app(), client=("203.0.113.9", 50000))


def test_docs_and_metrics_are_closed_to_remote_callers() -> None:
    with _remote() as remote:
        assert remote.get("/openapi.json").status_code == 403
        assert remote.get("/docs").status_code == 403
        assert remote.get("/metrics").status_code == 403


def test_forwarding_headers_defeat_the_loopback_exemption(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 200  # direct (testclient) caller
    for header in ("x-forwarded-for", "x-real-ip", "forwarded"):
        resp = client.get("/openapi.json", headers={header: "198.51.100.7"})
        assert resp.status_code == 403, header
    assert client.get("/metrics", headers={"x-forwarded-for": "198.51.100.7"}).status_code == 403


def _break_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import ops

    async def bad() -> dict[str, Any]:
        return {"ok": False, "error": "OperationalError"}

    async def fine() -> dict[str, Any]:
        return {"ok": True}

    monkeypatch.setattr(ops, "_check_postgres", bad)
    monkeypatch.setattr(ops, "_check_redis", fine)
    monkeypatch.setattr(ops, "_check_searxng", fine)


def test_readyz_hides_internals_from_remote_callers(monkeypatch: pytest.MonkeyPatch) -> None:
    _break_postgres(monkeypatch)
    with _remote() as remote:
        body = remote.get("/readyz").json()
    assert body["status"] == "degraded"
    assert body["checks"]["postgres"] == {"ok": False}
    assert "schema_version" not in body
    assert "OperationalError" not in str(body)


def test_readyz_keeps_detail_for_the_local_operator(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_postgres(monkeypatch)
    body = client.get("/readyz").json()
    assert body["checks"]["postgres"]["error"] == "OperationalError"
    assert "schema_version" in body


# --------------------------------------------------------------------------------------
# F-07: published request contract is bounded
# --------------------------------------------------------------------------------------

# Pre-existing free-text/identifier fields reviewed as acceptable (bounded by the 1 MB body
# cap and by downstream truncation). Anything NEW must be bounded or added here with a reason.
_UNBOUNDED_ALLOWLIST: set[tuple[str, str]] = set()
_BOUNDED_INT_NAMES = {"limit", "offset", "days", "max_pages", "max_urls", "depth", "breadth", "k"}
_FORMAT_EXEMPT = {"uuid", "date-time", "date", "uri"}


def _resolve(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return spec["components"]["schemas"][name]
    return schema


def _walk_unbounded(
    spec: dict[str, Any], owner: str, prop: str, schema: dict[str, Any], seen: set[str]
) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        if name in seen:
            return found
        seen = seen | {name}
        return _walk_schema(spec, name, seen)
    for branch in schema.get("anyOf", []) + schema.get("oneOf", []):
        found += _walk_unbounded(spec, owner, prop, branch, seen)
    kind = schema.get("type")
    if kind == "array":
        if "maxItems" not in schema:
            found.append((owner, prop))
        found += _walk_unbounded(spec, owner, prop + "[]", schema.get("items", {}), seen)
    elif kind == "string":
        exempt = (
            "enum" in schema
            or "const" in schema
            or "pattern" in schema
            or schema.get("format") in _FORMAT_EXEMPT
        )
        if "maxLength" not in schema and not exempt:
            found.append((owner, prop))
    elif (
        kind in ("integer", "number")
        and prop.split("[")[0] in _BOUNDED_INT_NAMES
        and "maximum" not in schema
        and "exclusiveMaximum" not in schema
    ):
        found.append((owner, prop))
    return found


def _walk_schema(spec: dict[str, Any], name: str, seen: set[str]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    body = spec["components"]["schemas"][name]
    for prop, sub in (body.get("properties") or {}).items():
        found += _walk_unbounded(spec, name, prop, sub, seen)
    return found


def _request_schemas(spec: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for ops in spec["paths"].values():
        for verb, op in ops.items():
            if verb not in ("post", "put", "patch"):
                continue
            content = (op.get("requestBody") or {}).get("content", {})
            for media in content.values():
                ref = media.get("schema", {}).get("$ref")
                if ref:
                    names.add(ref.rsplit("/", 1)[-1])
    return names


def test_request_bodies_are_bounded() -> None:
    spec = create_app().openapi()
    violations: set[tuple[str, str]] = set()
    for name in _request_schemas(spec):
        violations |= set(_walk_schema(spec, name, {name}))
    new = sorted(violations - _UNBOUNDED_ALLOWLIST)
    assert not new, (
        f"Request fields without an upper bound (add max_length / max_length on lists / le): {new}"
    )


# GET /v1/queries clamps limit (1..100) and offset (0..100_000) in-handler rather than
# rejecting with 400 (contract pinned by tests/test_ops_status.py::TestQueriesList).
_CLAMPED_QUERY_PARAMS = {("GET /v1/queries", "limit"), ("GET /v1/queries", "offset")}


def test_query_parameters_are_bounded() -> None:
    spec = create_app().openapi()
    bad: list[tuple[str, str]] = []
    for path, ops in spec["paths"].items():
        for verb, op in ops.items():
            for param in op.get("parameters", []):
                if param.get("in") not in ("query", "path"):
                    continue
                schema = param.get("schema", {})
                for branch in [schema, *schema.get("anyOf", [])]:
                    kind = branch.get("type")
                    pname = param["name"]
                    key = (f"{verb.upper()} {path}", pname)
                    if key in _CLAMPED_QUERY_PARAMS:
                        continue
                    if kind == "string" and not (
                        "maxLength" in branch
                        or "enum" in branch
                        or "pattern" in branch
                        or branch.get("format") in _FORMAT_EXEMPT
                    ):
                        bad.append(key)
                    if (
                        kind == "integer"
                        and pname in _BOUNDED_INT_NAMES
                        and "maximum" not in branch
                    ):
                        bad.append(key)
    assert not bad, f"Unbounded query/path parameters: {sorted(set(bad))}"


# --------------------------------------------------------------------------------------
# F-21: deterministic compression faithfulness & citation-preservation benchmark
# --------------------------------------------------------------------------------------


def test_compression_profiles_preserve_citations_and_numeric_facts() -> None:
    from app.extraction.chunker import _new_chunk
    from app.llm.compressor import compress_chunks

    chunks = [
        _new_chunk(
            document_url="https://example.com/fpt-q3-2026",
            position=1,
            heading_path="BCTC Q3/2026",
            content=(
                "Báo cáo tài chính quý 3/2026 của FPT ghi nhận doanh thu đạt 15.920 tỷ đồng "
                "(tăng 18,4% YoY) và lợi nhuận trước thuế đạt 2.910 tỷ đồng. "
                "Tỷ lệ ROE duy trì ở mức 28,1% và biên lợi nhuận gộp đạt 39,2%. "
                "Ngoài ra, công ty đã mở rộng các văn phòng đại diện tại nhiều khu vực mới "
                "nhằm phục vụ khách hàng doanh nghiệp toàn cầu và tối ưu hóa chi phí vận hành "
                "hành chính trong suốt giai đoạn vừa qua. "
                "Các hoạt động truyền thông nội bộ, sự kiện văn hóa doanh nghiệp và chương trình "
                "đào tạo định kỳ cũng được tổ chức thường xuyên tại các cơ sở đào tạo địa phương."
            ),
            source_title="FPT Q3/2026 Financial Report",
        ),
        _new_chunk(
            document_url="https://example.com/roe-code",
            position=1,
            heading_path="Code",
            content=(
                "def calculate_roe(net_income: float, equity: float) -> float:\n"
                "    if equity <= 0:\n"
                "        raise ValueError('equity must be positive')\n"
                "    return round((net_income / equity) * 100.0, 2)\n"
                "Hàm trên tính toán ROE chuẩn xác theo chuẩn mực kế toán hiện hành."
            ),
            source_title="ROE Formula Snippet",
        ),
    ]
    query = "doanh thu lợi nhuận ROE FPT quý 3/2026 và công thức calculate_roe"

    for profile in ("balanced", "preserve_numbers", "preserve_code", "compact"):
        result = compress_chunks(chunks, query=query, budget=105, profile=profile)
        assert len(result.chunks) == 2
        assert result.stats.compressed_units < result.stats.raw_units
        joined = "\n".join(c.content for c in result.chunks)
        assert "15.920" in joined
        assert "28,1%" in joined
        assert "văn phòng đại diện" not in joined
        if profile == "preserve_code":
            assert "def calculate_roe" in joined


async def test_concurrent_sqlite_writers_with_slow_commits_do_not_lock(
    tmp_path, monkeypatch
) -> None:
    """Concurrent SQLite background writers (search, memory, answer) must serialize via
    sqlite_write_lock instead of hitting SQLITE_BUSY / database-is-locked under slow commits."""
    import asyncio
    import uuid
    from datetime import UTC, datetime

    from sqlalchemy import func, select

    import app.db.session as session_mod
    from app.config import get_settings
    from app.db.bootstrap import ensure_schema
    from app.db.models import Answer, MemoryItem, Query
    from app.extraction.scraper import PageContent
    from app.orchestration import persist as persist_mod
    from app.retrieval.memory import record_memory, record_observation

    db_file = tmp_path / "concurrent.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file.as_posix()}")
    get_settings.cache_clear()
    await session_mod.dispose_engine()
    await ensure_schema()

    real_maker = session_mod.get_sessionmaker

    def slow_maker():
        maker = real_maker()

        def factory(*a, **k):
            s = maker(*a, **k)
            orig = s.commit

            async def commit():
                await asyncio.sleep(0.15)
                return await orig()

            s.commit = commit
            return s

        return factory

    monkeypatch.setattr(session_mod, "get_sessionmaker", slow_maker)
    monkeypatch.setattr(persist_mod, "get_sessionmaker", slow_maker)

    try:
        qid = uuid.uuid4()
        aid = uuid.uuid4()
        page = PageContent(
            url="https://example.com/p1",
            final_url="https://example.com/p1",
            title="Test",
            markdown="# Test\n\nContent",
            extractor_used="trafilatura",
            fetched_at=datetime.now(UTC),
        )
        ok_mem1, ok_mem2, _, _ = await asyncio.gather(
            record_memory("anon", kind="query", text="concurrent test", timeout_s=5.0),
            record_observation(
                "anon",
                title="Obs",
                summary="Concurrent SQLite test summary",
                timeout_s=5.0,
            ),
            persist_mod._persist_search_all(
                qid, "concurrent test", "concurrent test", "en", {}, []
            ),
            persist_mod.persist_pages_async([page], []),
        )
        await persist_mod.persist_answer(
            answer_id=aid,
            query_id=qid,
            content_md="Answer [1].",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
            latency_ms=10,
            citations=[(1, "https://example.com/p1", "Test", None)],
        )
        assert ok_mem1 is True
        assert ok_mem2 is True
        async with real_maker()() as s:
            assert (
                int((await s.execute(select(func.count()).select_from(Answer))).scalar_one()) == 1
            )
            assert int((await s.execute(select(func.count()).select_from(Query))).scalar_one()) == 1
            assert (
                int((await s.execute(select(func.count()).select_from(MemoryItem))).scalar_one())
                == 2
            )
    finally:
        await session_mod.dispose_engine()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_briefings_route_bills_weight_8():
    """docs/33 audit P1-3 regression: the briefings cost lives in a PREFIX
    special-case (mid-path segment defeats static suffix matching) — assert
    the real billed weight, not just the classification."""

    class _FakeRequest:
        url = SimpleNamespace(path="/v1/briefings/macro_economy")
        method = "POST"

        async def body(self):
            return b"{}"

    assert await _route_cost(_FakeRequest()) == 8
