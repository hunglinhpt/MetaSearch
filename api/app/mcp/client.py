"""MCP REST client + markdown formatters (docs/07-mcp-integration.md).

Thin layer, no business logic: tool input -> REST call -> markdown output.
Kept free of the ``mcp`` SDK so the tool behaviour is unit-testable without
the optional dependency; ``server.py`` wires these functions into FastMCP.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx

DEFAULT_API_URL = "http://localhost:8000"
_SHARED_CLIENT: httpx.AsyncClient | None = None
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
        # convention (connection setup per tool call). One module-level client
        # is reused; MCP processes are short-lived so explicit close is via
        # atexit-less GC — acceptable for a CLI-shaped client.
        global _SHARED_CLIENT
        try:
            client = _SHARED_CLIENT
        except NameError:
            client = None
        if client is None:
            _SHARED_CLIENT = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S)
            client = _SHARED_CLIENT
            resp = await client.request(
                method, f"{_api_url()}{path}", json=payload, headers=_headers()
            )
    except httpx.HTTPError as exc:
        raise MCPClientError(
            f"MetaSearch API unreachable at {_api_url()} ({type(exc).__name__})"
        ) from exc
    if resp.status_code >= 400:
        try:
            err = resp.json()["error"]
            message = f"{err.get('code', 'error')}: {err.get('message', resp.text[:200])}"
        except Exception:
            message = f"HTTP {resp.status_code}"
        raise MCPClientError(message)
    return resp.json()


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


def run_tool(name: str, kwargs: dict[str, Any]) -> Any:
    """Sync entry for tests/CLI; dispatches one tool call."""
    tools = {
        "web_search": web_search,
        "web_scrape": web_scrape,
        "web_search_and_scrape": web_search_and_scrape,
        "deep_research": deep_research,
        "research_status": research_status,
    }
    if name not in tools:
        raise MCPClientError(f"unknown tool: {name}")
    return asyncio.run(tools[name](**kwargs))
