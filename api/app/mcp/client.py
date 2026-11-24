"""MCP REST client + markdown formatters (docs/07-mcp-integration.md).

Thin layer, no business logic: tool input -> REST call -> markdown output.
Kept free of the ``mcp`` SDK so the tool behaviour is unit-testable without
the optional dependency; ``server.py`` wires these functions into FastMCP.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import threading
import weakref
from typing import Any

import httpx

DEFAULT_API_URL = "http://localhost:8000"
_SHARED_CLIENT: httpx.AsyncClient | None = None
_SHARED_CLIENT_LOOP: asyncio.AbstractEventLoop | None = None
_CLIENT_BY_LOOP: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient] = (
    weakref.WeakKeyDictionary()
)
_CLIENT_LOCK = threading.Lock()
DEFAULT_TIMEOUT_S = 120.0
_TOTAL_CHAR_BUDGET = 12_000  # docs/07 §5: keep the agent's context lean


class MCPClientError(Exception):
    """Friendly tool error surfaced to the agent."""


def _api_url() -> str:
    return os.environ.get("METASEARCH_API_URL", DEFAULT_API_URL).rstrip("/")


def _headers() -> dict[str, str]:
    key = os.environ.get("METASEARCH_API_KEY")
    return {"Authorization": f"Bearer {key}"} if key else {}


async def _call(method: str, path: str, payload: dict | None = None) -> Any:
    try:
        # Audit round 11 P3: a per-call AsyncClient violates the shared-pool
        # convention (connection setup per tool call). Keep per-loop clients
        # under a threading.Lock so concurrent threads or sequential asyncio.run()
        # calls never collide on a closed loop, while still honoring tests that
        # inject _SHARED_CLIENT directly (with _SHARED_CLIENT_LOOP=None).
        global _SHARED_CLIENT, _SHARED_CLIENT_LOOP
        loop = asyncio.get_running_loop()
        with _CLIENT_LOCK:
            try:
                injected = _SHARED_CLIENT
            except NameError:
                injected = None
            if (
                injected is not None
                and _SHARED_CLIENT_LOOP is None
                and not getattr(injected, "is_closed", False)
            ):
                client: httpx.AsyncClient = injected
            else:
                existing = _CLIENT_BY_LOOP.get(loop)
                if existing is None or getattr(existing, "is_closed", False):
                    created = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S)
                    _CLIENT_BY_LOOP[loop] = created
                    _SHARED_CLIENT = created
                    _SHARED_CLIENT_LOOP = loop
                    client = created
                else:
                    client = existing
        resp = await client.request(method, f"{_api_url()}{path}", json=payload, headers=_headers())
    except httpx.HTTPError as exc:
        raise MCPClientError(
            f"MetaSearch API unreachable at {_api_url()} ({type(exc).__name__})"
        ) from exc
    if resp.status_code >= 400:
        try:
            err = resp.json()["error"]
            message = f"{err.get('code', 'error')}: {err.get('message', resp.text[:200])}"
            if err.get("details"):
                message = f"{message} ({err['details']})"[:300]
        except Exception:
            message = f"HTTP {resp.status_code}"
        raise MCPClientError(message)
    try:
        return resp.json()
    except ValueError as exc:
        raise MCPClientError(
            f"MetaSearch API returned invalid JSON (HTTP {resp.status_code})"
        ) from exc


def _format_search(results: list[dict[str, Any]]) -> str:
    lines = []
    for r in results:
        engines = ", ".join(r.get("engines") or r.get("found_by") or [])
        lines.append(
            f"{r['rank']}. [{r['title'] or r['url']}]({r['url']}) — {r.get('snippet', '')}"
            f" (score {r.get('score', 0):.2f}, {engines})"
        )
    return "\n".join(lines) or "No results."


def _cap(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[:max_chars] + "\n… (truncated)"


# -- tools (docs/07 §2) -------------------------------------------------------


async def web_search(
    query: str,
    *,
    lang: str | None = None,
    k: int = 10,
    engines: list[str] | None = None,
    categories: list[str] | None = None,
    freshness_days: int | None = None,
) -> str:
    payload: dict[str, Any] = {"query": query, "k": k}
    if lang:
        payload["lang"] = lang
    if engines:
        payload["engines"] = engines
    if categories:
        payload["categories"] = categories
    if freshness_days:
        payload["freshness_days"] = freshness_days
    data = await _call("POST", "/v1/search", payload)
    return _cap(_format_search(data["results"]), 8_000)


async def web_scrape(urls: list[str], *, max_chars: int = 8000) -> str:
    data = await _call("POST", "/v1/scrape", {"urls": urls[:10]})
    blocks = []
    for item in data:
        if item.get("status") == "ok":
            blocks.append(
                f"### {item.get('title') or item['url']} ({item['url']})\n{item['markdown']}"
            )
        else:
            blocks.append(f"### {item['url']}\nFAILED: {item.get('error', item.get('status'))}")
    return _cap("\n\n".join(blocks), max(max_chars, 500))


async def web_search_and_scrape(
    query: str,
    *,
    lang: str | None = None,
    k: int = 5,
    scrape_top: int = 3,
    max_chars_per_page: int = 4000,
) -> str:
    payload: dict[str, Any] = {"query": query, "k": k}
    if lang:
        payload["lang"] = lang
    data = await _call("POST", "/v1/search", payload)
    results = data["results"][:scrape_top]
    search_md = _format_search(data["results"])
    if not results:
        return f"## Kết quả tìm kiếm\n{search_md}"

    urls = [r["url"] for r in results]
    pages = await _call("POST", "/v1/scrape", {"urls": urls})
    sections = []
    for page in pages:
        if page.get("status") == "ok":
            sections.append(
                f"### {page.get('title') or page['url']} ({page['url']})\n"
                f"{_cap(page.get('markdown', ''), max_chars_per_page)}"
            )
        else:
            sections.append(f"### {page['url']}\nFAILED: {page.get('error', page.get('status'))}")
    combined = f"## Kết quả tìm kiếm\n{search_md}\n\n## Nội dung trang\n" + "\n\n".join(sections)
    return _cap(combined, _TOTAL_CHAR_BUDGET)


async def deep_research(
    query: str,
    *,
    breadth: int = 4,
    depth: int = 2,
    max_time_s: int = 180,
) -> str:
    data = await _call(
        "POST",
        "/v1/research",
        {"query": query, "breadth": breadth, "depth": depth, "max_time_s": max_time_s},
    )
    # The agent follows up with the research_status tool; it never has to call REST itself.
    return json.dumps(
        {
            "research_id": data["research_id"],
            "status": data["status"],
            "next": "call research_status with this research_id until status is done or failed",
        },
        ensure_ascii=False,
    )


async def research_status(research_id: str, *, max_chars: int = _TOTAL_CHAR_BUDGET) -> str:
    """Status of a deep_research job; the report once it is done."""
    data = await _call("GET", f"/v1/research/{research_id}")
    out: dict[str, Any] = {
        "research_id": data.get("research_id", research_id),
        "status": data.get("status"),
        "progress": data.get("progress"),
    }
    if data.get("error"):
        out["error"] = data["error"]
    if data.get("report_md"):
        out["report_md"] = _cap(data["report_md"], max_chars)
        out["citations"] = data.get("citations", [])
    return json.dumps(out, ensure_ascii=False)


async def memory_search(
    query: str,
    *,
    kind: str | None = None,
    limit: int = 10,
) -> str:
    """Layer 1 Progressive Disclosure: search compact memory index (~40 tokens/item)."""
    from urllib.parse import urlencode

    params: dict[str, Any] = {"q": query, "limit": max(1, min(limit, 50))}
    if kind:
        params["kind"] = kind
    data = await _call("GET", f"/v1/memory/search?{urlencode(params)}")
    return json.dumps(data, ensure_ascii=False)


async def memory_timeline(
    *,
    anchor_id: str | None = None,
    before: int = 3,
    after: int = 3,
) -> str:
    """Layer 2 Progressive Disclosure: chronological context around anchor_id."""
    from urllib.parse import urlencode

    params: dict[str, Any] = {"before": before, "after": after}
    if anchor_id:
        params["anchor_id"] = anchor_id
    data = await _call("GET", f"/v1/memory/timeline?{urlencode(params)}")
    return json.dumps(data, ensure_ascii=False)


async def memory_get_observations(ids: list[str], *, max_chars: int = _TOTAL_CHAR_BUDGET) -> str:
    """Layer 3 Progressive Disclosure: fetch full memory details for selected IDs."""
    data = await _call("POST", "/v1/memory/observations", {"ids": ids[:50]})
    return _cap(json.dumps(data, ensure_ascii=False), max(max_chars, 500))


async def equity_sentiment(
    ticker: str,
    *,
    company_name: str | None = None,
    exchange: str | None = None,
    max_queries: int = 10,
    fetch_market_data: bool = False,
) -> str:
    """Omni-Channel Bilingual Equity Sentiment Engine (VI / EN) across 5 network layers."""
    payload: dict[str, Any] = {
        "ticker": ticker,
        "max_queries": max(1, min(max_queries, 22)),
        "fetch_market_data": fetch_market_data,
    }
    if company_name:
        payload["company_name"] = company_name
    if exchange:
        payload["exchange"] = exchange
    data = await _call("POST", "/v1/market/sentiment", payload)
    return json.dumps(data, ensure_ascii=False)


def _clean_str_list(items: list[str] | None, cap: int) -> list[str]:
    return [s.strip() for s in (items or []) if isinstance(s, str) and s.strip()][:cap]


def _bounded_json_dumps(data: dict[str, Any], trim_keys: tuple[str, ...]) -> str:
    dumped = json.dumps(data, ensure_ascii=False)
    if len(dumped) <= _TOTAL_CHAR_BUDGET:
        return dumped
    payload = copy.deepcopy(data)
    payload["truncated"] = True
    # Progressively shrink primary list arrays until JSON fits cleanly within MCP char budget.
    for key in trim_keys:
        val = payload.get(key)
        if isinstance(val, list) and len(val) > 20:
            payload[key] = val[:20]
    dumped = json.dumps(payload, ensure_ascii=False)
    while len(dumped) > _TOTAL_CHAR_BUDGET:
        shrunk = False
        for key in trim_keys:
            val = payload.get(key)
            if isinstance(val, list) and len(val) > 1:
                payload[key] = val[: max(1, len(val) // 2)]
                shrunk = True
            elif isinstance(val, list) and len(val) == 1 and key != trim_keys[0]:
                payload[key] = []
                shrunk = True
        if not shrunk:
            break
        dumped = json.dumps(payload, ensure_ascii=False)
    if len(dumped) > _TOTAL_CHAR_BUDGET:
        sre = payload.get("share_ratio_explanation")
        if isinstance(sre, dict):
            if isinstance(sre.get("matched_events"), list):
                sre["matched_events"] = []
            if isinstance(sre.get("explanation"), str):
                sre["explanation"] = sre["explanation"][:300]
        for aux_key in (
            "new_fingerprints",
            "new_event_ids",
            "warnings",
            "sources_failed",
            "sources_used",
            "symbols",
        ):
            if aux_key not in trim_keys and isinstance(payload.get(aux_key), list):
                payload[aux_key] = []
        dumped = json.dumps(payload, ensure_ascii=False)
    if len(dumped) > _TOTAL_CHAR_BUDGET:
        first_key = trim_keys[0]
        if isinstance(payload.get(first_key), list):
            payload[first_key] = payload[first_key][:1]
            for it in payload[first_key]:
                if isinstance(it, dict):
                    if isinstance(it.get("also_reported_by"), list):
                        it["also_reported_by"] = []
                    if isinstance(it.get("symbols"), list):
                        it["symbols"] = it["symbols"][:5]
                    for txt_k in ("title", "snippet", "detail", "url", "source_url"):
                        if isinstance(it.get(txt_k), str):
                            it[txt_k] = it[txt_k][:160]
        dumped = json.dumps(payload, ensure_ascii=False)
    if len(dumped) > _TOTAL_CHAR_BUDGET:
        first_key = trim_keys[0]
        if isinstance(payload.get(first_key), list):
            payload[first_key] = []
        dumped = json.dumps(payload, ensure_ascii=False)
    if len(dumped) > _TOTAL_CHAR_BUDGET:
        compact: dict[str, Any] = {"truncated": True}
        for k, v in payload.items():
            if k == "truncated":
                continue
            if isinstance(v, (bool, int, float)) or v is None:
                compact[k] = v
            elif isinstance(v, list):
                compact[k] = []
            elif isinstance(v, str):
                compact[k] = v[:200]
        dumped = json.dumps(compact, ensure_ascii=False)
    return dumped


async def market_news_wire(
    symbols: list[str] | None = None,
    *,
    lang: str = "auto",
    categories: list[str] | None = None,
    include_market_macro: bool = True,
    max_age_days: int = 7,
    max_items: int = 30,
    seen_fingerprints: list[str] | None = None,
    include_search_fallback: bool = False,
) -> str:
    """Aggregate bilingual market news wire with context-aware ticker matching and dedup."""
    payload: dict[str, Any] = {
        "symbols": _clean_str_list(symbols, 50),
        "lang": lang if lang in ("auto", "vi", "en") else "auto",
        "include_market_macro": include_market_macro,
        "max_age_days": max(1, min(max_age_days, 90)),
        "max_items": max(1, min(max_items, 100)),
        "include_search_fallback": include_search_fallback,
    }
    clean_cats = _clean_str_list(categories, 7)
    if categories is not None:
        payload["categories"] = clean_cats
    clean_fps = _clean_str_list(seen_fingerprints, 500)
    if clean_fps:
        payload["seen_fingerprints"] = clean_fps
    data = await _call("POST", "/v1/market/news-wire", payload)
    if not isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False)
    return _bounded_json_dumps(data, ("items", "new_fingerprints"))


async def market_events(
    symbols: list[str] | None = None,
    *,
    include_corporate_actions: bool = True,
    include_calendar: bool = True,
    include_listing: bool = True,
    include_insider: bool = True,
    include_regulatory: bool = True,
    lookback_days: int = 365,
    lookahead_days: int = 60,
    reported_share_ratio: float | None = None,
    seen_event_ids: list[str] | None = None,
) -> str:
    """Fetch corporate actions, ex-dividend calendar, listing/regulatory notices & ratio proof."""
    payload: dict[str, Any] = {
        "symbols": _clean_str_list(symbols, 30),
        "include_corporate_actions": include_corporate_actions,
        "include_calendar": include_calendar,
        "include_listing": include_listing,
        "include_insider": include_insider,
        "include_regulatory": include_regulatory,
        "lookback_days": max(1, min(lookback_days, 3650)),
        "lookahead_days": max(1, min(lookahead_days, 365)),
    }
    if reported_share_ratio is not None:
        if isinstance(reported_share_ratio, bool):
            raise MCPClientError(f"invalid reported_share_ratio: {reported_share_ratio!r}")
        try:
            ratio_f = float(reported_share_ratio)
        except (TypeError, ValueError) as exc:
            raise MCPClientError(f"invalid reported_share_ratio: {reported_share_ratio!r}") from exc
        if not math.isfinite(ratio_f) or ratio_f < 1e-4 or ratio_f > 1000.0:
            raise MCPClientError(f"reported_share_ratio out of bounds: {reported_share_ratio!r}")
        payload["reported_share_ratio"] = ratio_f
    clean_seen = _clean_str_list(seen_event_ids, 500)
    if clean_seen:
        payload["seen_event_ids"] = clean_seen
    data = await _call("POST", "/v1/market/events", payload)
    if not isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False)
    return _bounded_json_dumps(
        data,
        ("events", "risk_alerts", "dilution_alerts", "upcoming_ex_dates", "new_event_ids"),
    )


def run_tool(name: str, kwargs: dict[str, Any]) -> Any:
    """Sync entry for tests/CLI; dispatches one tool call."""
    tools: dict[str, Any] = {
        "web_search": web_search,
        "web_scrape": web_scrape,
        "web_search_and_scrape": web_search_and_scrape,
        "deep_research": deep_research,
        "research_status": research_status,
        "memory_search": memory_search,
        "memory_timeline": memory_timeline,
        "memory_get_observations": memory_get_observations,
        "equity_sentiment": equity_sentiment,
        "market_news_wire": market_news_wire,
        "market_events": market_events,
    }
    if name not in tools:
        raise MCPClientError(f"unknown tool: {name}")
    return asyncio.run(tools[name](**kwargs))


# -- v4.5.0 Trust & Insight tools (docs/34) ----------------------------------


async def verify_claim(
    claim: str,
    *,
    lang: str | None = None,
    freshness_days: int | None = None,
    use_llm: bool = False,
    max_evidence: int = 8,
) -> str:
    """Fact-check ONE claim: verdict + verbatim evidence quotes for/against it."""
    text = " ".join(str(claim or "").split())
    if len(text) < 8 or len(text) > 500:
        raise MCPClientError("claim must be 8..500 characters")
    payload: dict[str, Any] = {
        "claim": text,
        "use_llm": bool(use_llm),
        "max_evidence": max(1, min(int(max_evidence), 12)),
    }
    if lang:
        payload["lang"] = lang
    if freshness_days is not None:
        payload["freshness_days"] = max(1, min(int(freshness_days), 3650))
    data = await _call("POST", "/v1/verify", payload)
    if not isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False)
    return _bounded_json_dumps(data, ("evidence",))


async def event_timeline(
    topic: str,
    *,
    days: int = 30,
    lang: str | None = None,
    max_events: int = 20,
) -> str:
    """Chronological distinct events for a topic (dated sources only) + coverage lens."""
    text = " ".join(str(topic or "").split())
    if len(text) < 2 or len(text) > 300:
        raise MCPClientError("topic must be 2..300 characters")
    payload: dict[str, Any] = {
        "topic": text,
        "days": max(1, min(int(days), 365)),
        "max_events": max(1, min(int(max_events), 40)),
    }
    if lang:
        payload["lang"] = lang
    data = await _call("POST", "/v1/timeline", payload)
    if not isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False)
    return _bounded_json_dumps(data, ("events",))
