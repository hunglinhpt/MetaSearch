"""FastMCP server wiring (docs/07-mcp-integration.md, docs/01 FR-10).

Requires the optional dependency group ``.[mcp]`` (official ``mcp`` SDK).
Tool bodies live in ``client.py`` (pure REST+markdown, testable without the
SDK) — this module only maps them to MCP tool signatures.

Run modes:
    python -m app.mcp                          # stdio (Claude Desktop)
    python -m app.mcp --transport http --port 8002
"""

from __future__ import annotations

from typing import Any

from app.mcp.client import (
    MCPClientError,
)


def build_server():  # returns FastMCP; lazy so the SDK stays optional
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - optional dep
        raise SystemExit(
            "MCP SDK missing: install with `pip install '.[mcp]'` (or pip install mcp>=1.2)"
        ) from exc

    mcp = FastMCP("metasearch")

    @mcp.tool()
    async def web_search(
        query: str,
        lang: str | None = None,
        k: int = 10,
        engines: list[str] | None = None,
        categories: list[str] | None = None,
        freshness_days: int | None = None,
    ) -> str:
        """Search the web via 70+ engines through SearXNG (dedup + scored).

        Use when the agent needs a ranked list of links/snippets. For deep
        content prefer web_search_and_scrape; for reports use deep_research.
        """
        return await web_search(
            query,
            lang=lang,
            k=k,
            engines=engines,
            categories=categories,
            freshness_days=freshness_days,
        )

    @mcp.tool()
    async def web_scrape(urls: list[str], max_chars: int = 8000) -> str:
        """Fetch page content as clean markdown (SSRF-guarded, JS-rendered
        fallback). Use when the agent already has URLs to read."""
        return await web_scrape(urls, max_chars=max_chars)

    @mcp.tool()
    async def web_search_and_scrape(
        query: str,
        lang: str | None = None,
        k: int = 5,
        scrape_top: int = 3,
        max_chars_per_page: int = 4000,
    ) -> str:
        """Search then scrape the top results in one call — the primary tool
        for grounding an answer in real page content."""
        return await web_search_and_scrape(
            query,
            lang=lang,
            k=k,
            scrape_top=scrape_top,
            max_chars_per_page=max_chars_per_page,
        )

    @mcp.tool()
    async def deep_research(
        query: str, breadth: int = 4, depth: int = 2, max_time_s: int = 180
    ) -> dict[str, Any]:
        """Multi-iteration research report (30-180s). Returns a research_id to
        poll via GET /v1/research/{id} (or stream /events for progress)."""
        raw = await deep_research(query, breadth=breadth, depth=depth, max_time_s=max_time_s)
        return _loads(raw)

    return mcp


def _loads(raw: str) -> dict[str, Any]:
    import json

    return json.loads(raw)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="metasearch-mcp")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8002)
    args = parser.parse_args()

    server = build_server()
    try:
        if args.transport == "http":
            server.settings.host = args.host
            server.settings.port = args.port
            server.run(transport="streamable-http")
        else:
            server.run(transport="stdio")
    except MCPClientError as exc:  # pragma: no cover - startup only
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
