"""v4.5.0 Trust & Insight layer (docs/34): /v1/verify, /v1/timeline,
/v1/search/batch, security headers, and the guardrail wiring for all three."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.core.auth import static_route_cost
from app.core.errors import ErrorCode, MetaSearchError
from app.main import create_app
from app.orchestration import claim_verify as cv
from app.orchestration.timeline import build_timeline, country_of
from app.retrieval.models import RawResult
from tests.test_api_v1 import FakeAdapter, _page, stack  # noqa: F401  (fixture re-export)

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------------------
# Deterministic stance (assess_chunk)
# --------------------------------------------------------------------------------------

CLAIM = "Vietnam GDP grew 7.09% in 2024 according to the statistics office"


def test_supporting_chunk_with_matching_figure() -> None:
    a = cv.assess_chunk(
        CLAIM,
        "The statistics office said Vietnam GDP grew 7.09% in 2024. Exports also rose.",
    )
    assert a.stance == "supports"
    assert a.relevance >= cv.SUPPORT_MIN
    assert a.numeric_match == 1.0
    assert "7.09%" in a.quote


def test_on_topic_chunk_with_different_figure_is_not_support() -> None:
    a = cv.assess_chunk(
        CLAIM,
        "The statistics office said Vietnam GDP grew 6.5% in 2024 according to estimates.",
    )
    assert a.stance == "mentions"
    assert a.numeric_match == 0.0


def test_explicit_refutation_cue_refutes() -> None:
    a = cv.assess_chunk(
        "Drinking hot water cures the flu virus completely",
        "Doctors debunked the claim that drinking hot water cures the flu virus completely.",
    )
    assert a.stance == "refutes"
    assert a.refute_cue is not None and a.refute_cue.startswith("debunk")


def test_vietnamese_refutation_cue() -> None:
    a = cv.assess_chunk(
        "Ngân hàng Nhà nước tăng lãi suất điều hành thêm 2 điểm",
        "Ngân hàng Nhà nước bác bỏ thông tin tăng lãi suất điều hành thêm 2 điểm.",
    )
    assert a.stance == "refutes"


def test_claim_about_a_denial_is_not_refuted_by_the_denial() -> None:
    claim = "The company denied the merger rumours on Monday morning"
    a = cv.assess_chunk(claim, "On Monday morning the company denied the merger rumours.")
    assert a.refute_cue is None
    assert a.stance == "supports"


def test_polarity_flip_needs_near_verbatim_window() -> None:
    claim = "The central bank raised interest rates in November"
    a = cv.assess_chunk(claim, "The central bank did not raise interest rates in November.")
    assert a.polarity_flip is True
    assert a.stance == "refutes"


def test_not_only_connective_is_not_a_negation() -> None:
    claim = "FPT mở rộng thị trường Nhật Bản năm nay"
    a = cv.assess_chunk(
        claim, "FPT không chỉ mở rộng thị trường Nhật Bản năm nay mà còn sang Hàn Quốc."
    )
    assert a.polarity_flip is False
    assert a.stance == "supports"


def test_empty_or_off_topic_chunk() -> None:
    assert cv.assess_chunk(CLAIM, "").relevance == 0.0
    off = cv.assess_chunk(CLAIM, "A recipe for pho needs beef bones and star anise.")
    assert off.relevance < cv.RELEVANCE_MIN


# --------------------------------------------------------------------------------------
# Verdict aggregation
# --------------------------------------------------------------------------------------


def _ev(domain: str, stance: str, weight: float = 0.8, url: str | None = None) -> cv.EvidenceOut:
    return cv.EvidenceOut(
        n=1,
        url=url or f"https://{domain}/a",
        title="t",
        domain=domain,
        domain_tier="unknown",
        stance=stance,
        relevance=0.9,
        quote="q",
        numeric_match=None,
        refute_cue=None,
        polarity_flip=False,
        judge="lexical",
        published_at=None,
        fetched_at=None,
        weight=weight,
    )


def test_no_decisive_evidence_is_insufficient() -> None:
    v = cv.aggregate_verdict([_ev("a.com", "mentions")])
    assert v.verdict == "insufficient_evidence"
    assert v.confidence == 0.0
    assert v.counts["mentions"] == 1
    assert cv.aggregate_verdict([]).verdict == "insufficient_evidence"


def test_single_domain_support_is_flagged() -> None:
    v = cv.aggregate_verdict(
        [_ev("a.com", "supports"), _ev("a.com", "supports", url="https://a.com/b")]
    )
    assert v.verdict == "supported"
    assert v.single_source is True
    assert v.independent_domains == 1  # one site cannot stack votes


def test_broad_support_beats_one_refutation() -> None:
    ev = [_ev(d, "supports") for d in ("a.com", "b.org", "c.vn")] + [_ev("d.net", "refutes", 0.5)]
    v = cv.aggregate_verdict(ev)
    assert v.verdict == "supported"
    assert v.single_source is False
    assert 0 < v.confidence <= 1


def test_balanced_evidence_is_contested() -> None:
    v = cv.aggregate_verdict([_ev("a.com", "supports"), _ev("b.com", "refutes")])
    assert v.verdict == "contested"
    assert v.confidence == 0.0


def test_refuted() -> None:
    v = cv.aggregate_verdict([_ev("a.gov", "refutes", 0.95), _ev("b.com", "refutes")])
    assert v.verdict == "refuted"


def test_build_evidence_keeps_best_chunk_per_url_and_numbers() -> None:
    chunks = [
        {"url": "https://x.com/1", "title": "X", "content": "Unrelated pho recipe text here."},
        {
            "url": "https://gso.gov.vn/r",
            "title": "GSO",
            "content": "The statistics office said Vietnam GDP grew 7.09% in 2024.",
            "published_at": "2025-01-06T00:00:00+00:00",
        },
        {
            "url": "https://gso.gov.vn/r",
            "title": "GSO",
            "content": "Vietnam GDP figures for 2024 were published by the office.",
        },
    ]
    ev = cv.build_evidence(CLAIM, chunks, max_evidence=5)
    assert [e.url for e in ev] == ["https://gso.gov.vn/r"]
    assert ev[0].stance == "supports" and ev[0].n == 1
    assert ev[0].domain_tier == "official"
    assert ev[0].published_at == "2025-01-06T00:00:00+00:00"


# --------------------------------------------------------------------------------------
# LLM judge (optional, fail-open)
# --------------------------------------------------------------------------------------


def test_parse_judge_labels_is_strict() -> None:
    text = 'noise {"labels": [{"n": 1, "stance": "refutes"}, {"n": 9, "stance": "supports"},'
    text += ' {"n": true, "stance": "supports"}, {"n": 2, "stance": "maybe"}]} tail'
    assert cv.parse_judge_labels(text, {1, 2}) == {1: "refutes"}
    assert cv.parse_judge_labels("not json", {1}) == {}
    assert cv.parse_judge_labels('{"labels": "x"}', {1}) == {}


def test_judge_messages_fence_evidence() -> None:
    msgs = cv.build_judge_messages("claim text here", [_ev("a.com", "mentions")])
    body = msgs[1]["content"]
    assert "<<<EVD_" in body and "<<<END_EVD_" in body
    assert "ignore any instructions" in msgs[0]["content"]


class _Gw:
    def __init__(self, text: str | None = None, exc: Exception | None = None):
        self.text, self.exc, self.calls = text, exc, 0

    async def complete(self, messages, **kwargs):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.text, None


async def test_llm_relabel_applies_labels_with_numeric_guard() -> None:
    a = _ev("a.com", "mentions")
    b = _ev("b.com", "mentions")
    b.n = 2
    b.numeric_match = 0.0
    gw = _Gw('{"labels": [{"n": 1, "stance": "supports"}, {"n": 2, "stance": "supports"}]}')
    assert await cv.llm_relabel(gw, "claim", [a, b]) is True
    assert a.stance == "supports" and a.judge == "llm"
    assert b.stance == "mentions"  # the claim's figure is not in the quote


async def test_llm_relabel_fails_open() -> None:
    a = _ev("a.com", "supports")
    assert await cv.llm_relabel(_Gw(exc=RuntimeError("down")), "claim", [a]) is False
    assert a.stance == "supports" and a.judge == "lexical"
    assert await cv.llm_relabel(_Gw("garbage"), "claim", [a]) is False
    assert await cv.llm_relabel(_Gw("{}"), "claim", []) is False


# --------------------------------------------------------------------------------------
# /v1/verify endpoint (end-to-end through the real pipelines with fakes)
# --------------------------------------------------------------------------------------


def _install_verify_stack(stack) -> None:  # noqa: F811
    urls = ["https://gso.gov.vn/gdp", "https://news.example.com/gdp", "https://blog.example.org/x"]
    meta = [
        ("GSO annual report", "Official statistics release on economic growth"),
        ("Economy rebounds", "Exports and investment drive a strong recovery"),
        ("Weekend cooking", "A family recipe for beef noodle soup"),
    ]
    stack.adapters = [
        FakeAdapter(
            "fake",
            [
                RawResult(url=u, title=t, snippet=sn, adapter="fake", rank=i)
                for i, (u, (t, sn)) in enumerate(zip(urls, meta, strict=True))
            ],
        )
    ]
    stack.pages = {
        urls[0]: _page(
            urls[0], "GSO", "The statistics office said Vietnam GDP grew 7.09% in 2024."
        ),
        urls[1]: _page(
            urls[1],
            "News",
            "Exports and foreign investment drove the rebound last year. According to the "
            "statistics office, Vietnam GDP grew 7.09% in 2024, beating most forecasts.",
        ),
        urls[2]: _page(urls[2], "Blog", "My favourite pho recipe uses star anise and beef."),
    }
    stack.install()


def test_verify_endpoint_supported(stack) -> None:  # noqa: F811
    _install_verify_stack(stack)
    client = TestClient(create_app())
    resp = client.post("/v1/verify", json={"claim": CLAIM})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verdict"] == "supported"
    assert body["judge"] == "lexical"
    assert body["independent_domains"] == 2
    assert body["single_source"] is False
    assert all(e["quote"] for e in body["evidence"])
    assert {e["domain"] for e in body["evidence"]} == {"gso.gov.vn", "news.example.com"}
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_verify_endpoint_llm_judge_fails_open(stack, monkeypatch) -> None:  # noqa: F811
    _install_verify_stack(stack)

    async def boom(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr(stack.gateway, "complete", boom, raising=False)
    client = TestClient(create_app())
    resp = client.post("/v1/verify", json={"claim": CLAIM, "use_llm": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["judge"] == "lexical"
    assert resp.json()["verdict"] == "supported"


@pytest.mark.parametrize(
    "body",
    [
        {"claim": "short"},
        {"claim": "two words"},
        {"claim": "x" * 501},
        {"claim": CLAIM, "k": 13},
        {"claim": CLAIM, "max_evidence": 0},
        {"claim": CLAIM, "include_domains": ["a.com"] * 21},
    ],
)
def test_verify_request_is_bounded(client: TestClient, body: dict) -> None:
    assert client.post("/v1/verify", json=body).status_code in (400, 422)


# --------------------------------------------------------------------------------------
# Timeline (pure + endpoint)
# --------------------------------------------------------------------------------------


def _r(title, snippet, domain, pub, score=0.5):
    return SimpleNamespace(
        title=title,
        snippet=snippet,
        url=f"https://{domain}/{abs(hash(title)) % 10_000}",
        domain=domain,
        published_at=pub,
        score=score,
    )


def test_build_timeline_clusters_orders_and_counts() -> None:
    story = "Hội nghị trung ương bàn về chiến lược phát triển kinh tế số quốc gia giai đoạn mới"
    results = [
        _r("Kinh tế số: chiến lược mới", story, "vnexpress.net", NOW - timedelta(days=2)),
        _r("Kinh tế số: chiến lược mới", story, "www.tuoitre.vn", NOW - timedelta(days=3)),
        _r(
            "Fed holds rates steady",
            "The Federal Reserve kept interest rates unchanged citing inflation data",
            "reuters.com",
            NOW - timedelta(days=5),
        ),
        _r("Undated item", "no date at all", "example.com", None),
        _r("Ancient", "very old story about something else", "old.com", NOW - timedelta(days=90)),
        _r("From the future", "clock skew feed", "skew.com", NOW + timedelta(days=2)),
    ]
    out = build_timeline(results, now=NOW, days=30, max_events=10)
    assert out["undated_dropped"] == 1
    assert out["out_of_window_dropped"] == 2
    assert out["dated_results"] == 3
    assert out["events_total"] == 2
    events = out["events"]
    assert events[0]["title"] == "Fed holds rates steady"  # oldest first
    vn = events[1]
    assert vn["sources_count"] == 2
    # Event date = FIRST report (3 days ago), last update = latest member.
    assert vn["first_reported_at"] == (NOW - timedelta(days=3)).isoformat()
    assert vn["last_update_at"] == (NOW - timedelta(days=2)).isoformat()
    cov = out["coverage"]
    assert cov["distinct_domains"] == 3
    # vnexpress.net / reuters.com are generic TLDs; only tuoitre.vn is a VN ccTLD.
    assert cov["by_country"] == {"global": 2, "VN": 1}
    assert sum(d["count"] for d in cov["volume_by_day"]) == 3


def test_build_timeline_max_events_keeps_most_covered() -> None:
    stories = [
        ("Fed holds rates", "Federal Reserve keeps benchmark interest rate unchanged"),
        ("Typhoon hits coast", "Strong storm makes landfall bringing flooding to provinces"),
        ("Chip export rules", "New semiconductor export controls announced by commerce agency"),
        ("Football final", "National team wins regional championship after penalty shootout"),
        ("Rice prices climb", "Global rice market sees highest prices in a decade on supply"),
    ]
    results = [
        _r(t, body, f"s{i}.com", NOW - timedelta(days=i + 1)) for i, (t, body) in enumerate(stories)
    ]
    out = build_timeline(results, now=NOW, days=30, max_events=2)
    assert len(out["events"]) == 2
    assert out["events_total"] == 5
    assert out["events"][0]["first_reported_at"] <= out["events"][1]["first_reported_at"]


def test_country_of() -> None:
    assert country_of("vnexpress.net") == "global"
    assert country_of("baochinhphu.vn") == "VN"
    assert country_of("bbc.co.uk") == "GB"
    assert country_of("localhost") == "unknown"
    assert country_of("") == "unknown"


def test_timeline_endpoint(stack) -> None:  # noqa: F811
    pub = datetime.now(UTC) - timedelta(days=1)
    stack.adapters = [
        FakeAdapter(
            "fake",
            [
                RawResult(
                    url="https://reuters.com/a",
                    title="Fed holds rates",
                    snippet="The Federal Reserve kept rates unchanged",
                    adapter="fake",
                    published_at=pub,
                ),
                RawResult(
                    url="https://example.com/b",
                    title="Undated",
                    snippet="no date",
                    adapter="fake",
                ),
            ],
        )
    ]
    stack.install()
    client = TestClient(create_app())
    resp = client.post("/v1/timeline", json={"topic": "fed rates", "days": 7})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["topic"] == "fed rates"
    assert body["undated_dropped"] == 1
    assert len(body["events"]) == 1
    assert body["events"][0]["sources"][0]["domain_tier"]


def test_timeline_all_engines_failed_is_503(stack) -> None:  # noqa: F811
    stack.adapters = [FakeAdapter("fake", [], fail=True)]
    stack.install()
    client = TestClient(create_app())
    resp = client.post("/v1/timeline", json={"topic": "anything"})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "no_results"


@pytest.mark.parametrize(
    "body", [{"topic": "x"}, {"topic": "ok topic", "days": 366}, {"topic": "ok", "max_events": 41}]
)
def test_timeline_request_is_bounded(client: TestClient, body: dict) -> None:
    assert client.post("/v1/timeline", json=body).status_code in (400, 422)


# --------------------------------------------------------------------------------------
# /v1/search/batch
# --------------------------------------------------------------------------------------


class _BatchPipeline:
    def __init__(self):
        self.queries: list[str] = []

    async def run(self, params):
        self.queries.append(params.query)
        if params.query == "boom":
            raise MetaSearchError(ErrorCode.NO_RESULTS, "nothing")
        if params.query == "crash":
            raise RuntimeError("unexpected")
        r = SimpleNamespace(
            url=f"https://e.com/{params.query}",
            title=params.query,
            snippet="s",
            score=0.5,
            consensus=1,
            adapters=["fake"],
            engines=["fake"],
            domain="e.com",
            domain_tier="unknown",
            published_at=None,
            source_url=None,
            source_name=None,
            data_kind=None,
            as_of=None,
        )
        return SimpleNamespace(
            results=[r] * 3,
            statuses=[SimpleNamespace(status="ok")],
            complete=True,
            cached=False,
        )


def test_search_batch_is_partial(monkeypatch) -> None:
    fake = _BatchPipeline()
    monkeypatch.setattr(deps, "get_search_pipeline", lambda: fake)
    client = TestClient(create_app())
    resp = client.post(
        "/v1/search/batch",
        json={"queries": [{"query": "alpha", "k": 2}, {"query": "boom"}, {"query": "crash"}]},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["index"] for i in items] == [0, 1, 2]
    assert items[0]["ok"] is True and len(items[0]["results"]) == 2
    assert items[0]["results"][0]["rank"] == 1
    assert items[1]["ok"] is False and items[1]["error"]["code"] == "no_results"
    assert items[2]["ok"] is False and items[2]["error"]["code"] == "internal"
    assert sorted(fake.queries) == ["alpha", "boom", "crash"]


def test_search_batch_is_bounded(client: TestClient) -> None:
    too_many = {"queries": [{"query": f"q{i}"} for i in range(6)]}
    assert client.post("/v1/search/batch", json=too_many).status_code in (400, 422)
    assert client.post("/v1/search/batch", json={"queries": []}).status_code in (400, 422)


async def test_search_batch_bills_one_unit_per_query() -> None:
    import json as _json

    from starlette.requests import Request

    from app.core.auth import _route_cost

    body = _json.dumps({"queries": [{"query": "a"}, {"query": "b"}, {"query": "c"}]}).encode()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    req = Request(
        {"type": "http", "method": "POST", "path": "/v1/search/batch", "headers": []}, receive
    )
    assert await _route_cost(req) == 3


def test_new_routes_are_weighted() -> None:
    assert static_route_cost("POST", "/v1/verify") == 4
    assert static_route_cost("POST", "/v1/timeline") == 2
    # The GET memory timeline is NOT billed as a news fan-out.
    assert static_route_cost("GET", "/v1/memory/timeline") is None


# --------------------------------------------------------------------------------------
# Security headers
# --------------------------------------------------------------------------------------


def test_security_headers_on_api_and_docs(client: TestClient) -> None:
    resp = client.get("/v1/meta")
    h = resp.headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "no-referrer"
    assert "microphone=(self)" in h["permissions-policy"]
    assert h["content-security-policy"].startswith("default-src 'none'")
    assert "strict-transport-security" not in h  # off unless configured
    health = client.get("/healthz")
    assert health.headers["x-content-type-options"] == "nosniff"


def test_hsts_only_when_configured(monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setenv("HSTS_MAX_AGE_S", "31536000")
    get_settings.cache_clear()
    try:
        c = TestClient(create_app())
        assert "max-age=31536000" in c.get("/v1/meta").headers["strict-transport-security"]
    finally:
        get_settings.cache_clear()


async def test_security_headers_keep_route_values_and_skip_non_http() -> None:
    from app.core.middleware import SecurityHeadersMiddleware

    sent: list[dict] = []

    async def app(scope, receive, send):
        if scope["type"] != "http":
            sent.append({"type": "passthrough"})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"x-frame-options", b"SAMEORIGIN")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message):
        sent.append(message)

    mw = SecurityHeadersMiddleware(app)
    await mw({"type": "http", "path": "/ui"}, None, send)
    headers = dict(sent[0]["headers"])
    assert headers[b"x-frame-options"] == b"SAMEORIGIN"  # route value wins
    assert b"content-security-policy" not in headers  # UI paths: no API CSP
    await mw({"type": "lifespan"}, None, send)
    assert sent[-1] == {"type": "passthrough"}


class _NoContextPipeline:
    def __init__(self, statuses: dict[str, str]):
        self.statuses = statuses

    async def prepare_context(self, params, **kwargs):
        raise MetaSearchError(
            ErrorCode.NO_RESULTS, "nothing", details={"engines_status": self.statuses}
        )


def test_verify_unscrapable_is_insufficient_not_503(monkeypatch) -> None:
    monkeypatch.setattr(deps, "get_answer_pipeline", lambda: _NoContextPipeline({"ddg": "ok"}))
    resp = TestClient(create_app()).post("/v1/verify", json={"claim": CLAIM})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verdict"] == "insufficient_evidence"
    assert body["complete"] is False
    assert body["evidence"] == []


def test_verify_all_engines_down_is_503(monkeypatch) -> None:
    monkeypatch.setattr(
        deps, "get_answer_pipeline", lambda: _NoContextPipeline({"ddg": "error", "bing": "timeout"})
    )
    resp = TestClient(create_app()).post("/v1/verify", json={"claim": CLAIM})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "no_results"


# --------------------------------------------------------------------------------------
# Audit round 2 regressions
# --------------------------------------------------------------------------------------


def test_unpunctuated_chunk_is_windowed_not_one_giant_sentence() -> None:
    # A long list with no full stops that merely contains the claim's words
    # scattered far apart must not count as support.
    filler = " ".join(f"item{i}" for i in range(200))
    content = f"Vietnam {filler} GDP {filler} grew {filler} 7.09% {filler} 2024 statistics office"
    a = cv.assess_chunk(CLAIM, content)
    assert a.stance != "supports"
    assert len(a.quote) <= cv.QUOTE_MAX_CHARS


def test_verify_response_tolerates_very_long_urls() -> None:
    from app.api.v1.schemas import VerifyEvidenceOut

    long_url = "https://example.com/" + "a" * 5000
    ev = _ev("example.com", "supports", url=long_url).to_dict()
    assert VerifyEvidenceOut(**ev).url == long_url


def test_search_batch_gives_each_item_its_own_budget(monkeypatch) -> None:
    from app.api.v1 import search as search_mod

    created: list[object] = []
    real = search_mod.create_budget_for_route

    def spy(*a, **k):
        b = real(*a, **k)
        created.append(b)
        return b

    monkeypatch.setattr(search_mod, "create_budget_for_route", spy)
    monkeypatch.setattr(deps, "get_search_pipeline", lambda: _BatchPipeline())
    resp = TestClient(create_app()).post(
        "/v1/search/batch", json={"queries": [{"query": f"q{i}"} for i in range(4)]}
    )
    assert resp.status_code == 200
    assert len(created) == 4
    assert len({id(b) for b in created}) == 4


def test_vietnamese_decimal_comma_matches_english_decimal_point() -> None:
    # Audit round 3: "7,09%" (VI) vs "7.09%" (EN) must count as the same figure.
    a = cv.assess_chunk(
        "GDP Việt Nam năm 2024 tăng 7,09% theo Tổng cục Thống kê",
        "Theo Tổng cục Thống kê, GDP Việt Nam năm 2024 tăng 7.09% so với năm trước.",
    )
    assert a.numeric_match == 1.0
    assert a.stance == "supports"
    assert cv._canon_num("1.000 tỷ và 1,000 tỷ") == "1000 tỷ và 1000 tỷ"


def test_aggregator_wrapped_evidence_resolves_to_publisher_domain() -> None:
    # Live smoke 2026-11-24: Google News links must count per publisher.
    body = "The statistics office said Vietnam GDP grew 7.09% in 2024."
    chunks = [
        {
            "url": "https://news.google.com/rss/articles/AAA",
            "source_url": "https://laodong.vn",
            "title": "Lao Dong",
            "content": body,
        },
        {
            "url": "https://news.google.com/rss/articles/BBB",
            "source_url": "https://www.vietnamplus.vn",
            "title": "Vietnamplus",
            "content": body + " Exports rose.",
        },
    ]
    ev = cv.build_evidence(CLAIM, chunks, max_evidence=5)
    assert {e.domain for e in ev} == {"laodong.vn", "vietnamplus.vn"}
    assert cv.aggregate_verdict(ev).independent_domains == 2


# --------------------------------------------------------------------------------------
# Audit round 4 (live adversarial probe 2026-11-24) regressions
# --------------------------------------------------------------------------------------


def test_percentage_matching_is_token_exact() -> None:
    a = cv.assess_chunk(
        "GDP Việt Nam năm 2024 tăng 9,5%",
        "GDP Việt Nam năm 2024 tăng 7,09%, quý IV tăng 6,95% so với cùng kỳ.",
    )
    assert a.numeric_match == 0.0
    assert a.stance != "supports"
    assert cv._numeric_share(["95"], "in 1995 it rose") == 0.0
    assert cv._numeric_share(["95%"], "it rose 95 % last year") == 1.0
    assert cv._numeric_share(["1000"], "khoảng 1.000 tỷ đồng") == 1.0


def test_hearsay_window_is_never_support() -> None:
    claim = "Drinking bleach cures COVID-19 infection"
    a = cv.assess_chunk(
        claim, "Posts on social media claimed that drinking bleach cures COVID-19 infection."
    )
    assert a.hearsay is True
    assert a.stance == "mentions"
    b = cv.assess_chunk(claim, "Myth: drinking bleach cures COVID-19 infection. It is poison.")
    assert b.stance == "refutes"


def test_claim_phrased_as_rumour_is_not_self_hearsay() -> None:
    claim = "Tin đồn công ty X phá sản lan truyền trên mạng tuần này"
    a = cv.assess_chunk(claim, "Tin đồn công ty X phá sản lan truyền trên mạng tuần này.")
    assert a.hearsay is False
    assert a.stance == "supports"


async def test_llm_cannot_upgrade_hearsay_to_support() -> None:
    e = _ev("a.com", "mentions")
    e.hearsay = True
    gw = _Gw('{"labels": [{"n": 1, "stance": "supports"}]}')
    assert await cv.llm_relabel(gw, "claim", [e]) is True
    assert e.stance == "mentions"


def test_answer_numeric_fidelity_is_token_exact() -> None:
    # Pre-existing bug fixed in v4.5.0: substring matching let fabricated
    # figures pass /v1/answer's numeric-fidelity check.
    from app.extraction.chunker import Chunk
    from app.llm.citations import verify_numeric_fidelity

    c = Chunk(
        id="1",
        document_url="u",
        source_title="t",
        heading_path="",
        position=1,
        content="Quy IV tang 6,95% va nam 1995 dat 2.095 ty, von $5B, gia 1,000 USD",
        word_count=12,
    )
    score, unverified = verify_numeric_fidelity("GDP tang 95% [1]. Doanh thu 95 ty [1].", [(1, c)])
    assert score == 0.0 and "95%" in unverified
    ok, missing = verify_numeric_fidelity(
        "Von $5B [1]. Quy IV tang 6.95% [1]. Gia 1.000 USD [1].", [(1, c)]
    )
    assert ok == 1.0 and missing == []
