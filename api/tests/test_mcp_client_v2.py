"""MCP client tool coverage round 2 (docs/07 §2, docs/30): memory tools,
research status, market wire/events bounded JSON shrinking, dispatch."""

from __future__ import annotations

import json

import pytest

from app.mcp import client as mcp


def _fake_call(method: str, path: str, payload=None):
    async def _impl(expected_method: str, expected_path_prefix: str, response):
        async def call(method, path, payload=None):
            assert method == expected_method
            assert path.startswith(expected_path_prefix)
            return response

        return call

    return _impl


@pytest.fixture
def record_calls(monkeypatch):
    """Replace mcp._call with a recorder; returns (calls, set_response)."""
    calls: list[tuple[str, str, dict | None]] = []

    def install(response):
        async def fake_call(method, path, payload=None):
            calls.append((method, path, payload))
            return response

        monkeypatch.setattr(mcp, "_call", fake_call)
        return calls

    return install


# --- memory tools (progressive disclosure layers) ---------------------------------


@pytest.mark.asyncio
async def test_memory_search_builds_query_params(record_calls):
    calls = record_calls({"items": [{"id": "m1"}]})
    out = await mcp.memory_search("lãi suất", kind="observation", limit=999)
    assert json.loads(out) == {"items": [{"id": "m1"}]}
    method, path, _payload = calls[0]
    assert method == "GET" and "/v1/memory/search" in path
    assert "q=l%C3%A3i+su%E1%BA%A5t" in path or "q=lãi suất" in path
    assert "kind=observation" in path
    assert "limit=50" in path  # clamped to the 50 cap


@pytest.mark.asyncio
async def test_memory_timeline_with_anchor(record_calls):
    calls = record_calls({"items": [], "anchors": []})
    await mcp.memory_timeline(anchor_id="obs-42", before=2, after=2)
    _method, path, _payload = calls[0]
    assert "/v1/memory/timeline" in path
    assert "anchor_id=obs-42" in path
    assert "before=2" in path and "after=2" in path


@pytest.mark.asyncio
async def test_memory_get_observations_caps_ids_and_chars(record_calls):
    calls = record_calls({"observations": [{"id": str(i)} for i in range(100)]})
    out = await mcp.memory_get_observations([str(i) for i in range(80)], max_chars=200)
    _method, _path, payload = calls[0]
    assert len(payload["ids"]) == 50  # capped at 50 ids
    assert len(out) <= 520  # bounded by the max(max_chars, 500) cap + notice


# --- research status --------------------------------------------------------------


@pytest.mark.asyncio
async def test_research_status_done_with_report(record_calls):
    record_calls(
        {
            "research_id": "r-1",
            "status": "done",
            "progress": 100,
            "report_md": "Báo cáo " + "dài " * 5000,
            "citations": [{"n": 1, "url": "https://a"}],
        }
    )
    out = await mcp.research_status("r-1", max_chars=300)
    parsed = json.loads(out)
    assert parsed["status"] == "done"
    assert len(parsed["report_md"]) <= 300 + len("\n… (truncated)")
    assert parsed["citations"][0]["url"] == "https://a"


@pytest.mark.asyncio
async def test_research_status_failed_includes_error(record_calls):
    record_calls({"research_id": "r-2", "status": "failed", "error": "timeout"})
    parsed = json.loads(await mcp.research_status("r-2"))
    assert parsed["error"] == "timeout"
    assert "report_md" not in parsed


# --- market wire + events: bounded JSON -------------------------------------------


def test_bounded_json_dumps_small_payload_passthrough():
    data = {"items": [{"title": "x"}], "new_fingerprints": ["f1"]}
    out = mcp._bounded_json_dumps(data, ("items", "new_fingerprints"))
    assert json.loads(out) == data
    assert "truncated" not in out


def test_bounded_json_dumps_shrinks_to_budget():
    big_item = {
        "title": "t" * 500,
        "snippet": "s" * 500,
        "symbols": ["A", "B", "C"],
        "url": "u" * 50,
    }
    data = {
        "items": [big_item] * 50,
        "new_fingerprints": [f"f{i}" for i in range(50)],
        "sources_used": ["x"],
    }
    out = mcp._bounded_json_dumps(data, ("items", "new_fingerprints"))
    assert len(out) <= mcp._TOTAL_CHAR_BUDGET
    assert json.loads(out)["truncated"] is True


def test_bounded_json_dumps_compact_fallback():
    # a payload that cannot be shrunk by list trimming alone
    huge_str = "z" * (mcp._TOTAL_CHAR_BUDGET + 10_000)
    data = {"items": [{"title": huge_str}], "detail": huge_str}
    out = mcp._bounded_json_dumps(data, ("items",))
    assert len(out) <= mcp._TOTAL_CHAR_BUDGET
    parsed = json.loads(out)
    assert parsed.get("truncated") is True


@pytest.mark.asyncio
async def test_market_news_wire_payload_bounds(record_calls):
    calls = record_calls(
        {"items": [{"title": "T", "url": "u", "rank": 1}], "sources_used": ["vietstock_corporate"]}
    )
    out = await mcp.market_news_wire(
        symbols=["  FPT  ", "", 123, "MSFT"],
        lang="zh",  # invalid -> auto
        categories=["news", "  ", 5],
        max_age_days=999,
        max_items=5000,
        seen_fingerprints=[" fp1 ", "", "fp2"],
    )
    _method, _path, payload = calls[0]
    assert payload["symbols"] == ["FPT", "MSFT"]
    assert payload["lang"] == "auto"
    assert payload["categories"] == ["news"]
    assert payload["max_age_days"] == 90
    assert payload["max_items"] == 100
    assert payload["seen_fingerprints"] == ["fp1", "fp2"]
    assert json.loads(out)["items"][0]["title"] == "T"


@pytest.mark.asyncio
async def test_market_news_wire_non_dict_passthrough(record_calls):
    record_calls(["unexpected"])
    out = await mcp.market_news_wire(["FPT"])
    assert json.loads(out) == ["unexpected"]


@pytest.mark.asyncio
async def test_market_events_payload_bounds(record_calls):
    calls = record_calls({"events": [{"event_id": "e1", "title": "T"}], "symbols": ["FPT"]})
    out = await mcp.market_events(
        symbols=[" fpt ", "!!", "HPG"],
        lookback_days=10_000,
        reported_share_ratio=1.2,
        seen_event_ids=[" e0 "],
    )
    _method, _path, payload = calls[0]
    # _clean_str_list only strips whitespace + caps the count — no uppercasing
    assert payload["symbols"] == ["fpt", "!!", "HPG"]
    assert payload["lookback_days"] == 3650  # clamped to the 10-year bound
    assert payload["seen_event_ids"] == ["e0"]
    assert json.loads(out)["events"][0]["event_id"] == "e1"


# --- dispatch + scrape formatting -------------------------------------------------


@pytest.mark.asyncio
async def test_web_scrape_formats_blocks(record_calls):
    record_calls(
        [
            {"url": "https://a", "status": "ok", "title": "A Doc", "markdown": "content a"},
            {"url": "https://b", "status": "failed", "error": "blocked"},
        ]
    )
    out = await mcp.web_scrape(["https://a", "https://b"])
    assert "### A Doc (https://a)" in out
    assert "FAILED: blocked" in out


@pytest.mark.asyncio
async def test_web_search_and_scrape_no_results(record_calls):
    record_calls({"results": []})
    out = await mcp.web_search_and_scrape("q")
    assert out.startswith("## Kết quả tìm kiếm")
    assert "No results." in out


@pytest.mark.asyncio
async def test_equity_sentiment_optional_fields(record_calls):
    calls = record_calls({"sentiment_summary": {"market_regime": "MOMENTUM_MARKUP"}})
    await mcp.equity_sentiment("fpt", company_name="FPT Corp", exchange="HOSE", max_queries=99)
    _method, _path, payload = calls[0]
    assert payload["ticker"] == "fpt"
    assert payload["company_name"] == "FPT Corp"
    assert payload["max_queries"] == 22  # clamped
    assert payload["exchange"] == "HOSE"


def test_run_tool_dispatch_and_unknown(monkeypatch):
    captured: dict = {}

    async def fake_web_scrape(urls, *, max_chars=8000):
        captured["urls"] = urls
        return "ok"

    monkeypatch.setattr(mcp, "web_scrape", fake_web_scrape)
    assert mcp.run_tool("web_scrape", {"urls": ["https://a"]}) == "ok"
    assert captured["urls"] == ["https://a"]
    with pytest.raises(mcp.MCPClientError):
        mcp.run_tool("no_such_tool", {})


@pytest.mark.asyncio
async def test_deep_research_returns_followup_hint(record_calls):
    record_calls({"research_id": "r-9", "status": "queued"})
    parsed = json.loads(await mcp.deep_research("câu hỏi", breadth=6, depth=3))
    assert parsed["research_id"] == "r-9"
    assert "research_status" in parsed["next"]
