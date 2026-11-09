"""MCP server with the real SDK (the optional ``mcp`` extra). CI installs it; locally it skips."""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

EXPECTED = {"web_search", "web_scrape", "web_search_and_scrape", "deep_research", "research_status"}


async def test_server_registers_every_tool():
    from app.mcp.server import build_server

    server = build_server()
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert names >= EXPECTED, sorted(EXPECTED - names)


async def test_research_status_tool_goes_through_the_client(monkeypatch):
    from app.mcp import client as mcp_client
    from app.mcp.server import build_server

    async def fake_call(method, path, payload=None):
        return {"research_id": "r9", "status": "done", "progress": 1.0, "report_md": "# R"}

    monkeypatch.setattr(mcp_client, "_call", fake_call)
    server = build_server()
    result = await server.call_tool("research_status", {"research_id": "r9"})
    text = str(result)
    assert "r9" in text and "done" in text
