# ADR-006 — FastMCP cho MCP server

**Status:** Accepted · **Date:** 2025 · **Liên quan:** [docs/07](../07-mcp-integration.md), [docs/02 §2](../02-system-architecture.md)

## Context

AI Agent (Claude Desktop, IDE, agent framework) cần tool search/scrape/research theo chuẩn MCP. Tự implement protocol JSON-RPC của MCP là lặp code và dễ lệch spec (protocol thay đổi nhanh).

## Decision

**FastMCP** (Python) expose 4 tools: `web_search`, `web_scrape`, `web_search_and_scrape`, `deep_research`. Transport: stdio (local) + HTTP (remote). MCP server gọi nội bộ vào FastAPI (cùng logic, không nhân bản pipeline).

## Rationale

- FastMCP cùng hệ sinh thái Python, decorator-based, tự sinh JSON Schema cho tool.
- Tách interface agent khỏi API user: agent dùng tool ngữ nghĩa cao (`web_search_and_scrape` = 1 call), user dùng REST/SSE chi tiết.
- Khớp pattern `searxng-crawl4ai-mcp` đã tham chiếu.

## Alternatives

| Phương án | Vì sao không chọn |
|---|---|
| Agent tự gọi REST | Agent phải tự ghép search + scrape + lọc; tốn token, dễ sai |
| Tự viết MCP server thuần | Lặp code protocol, theo kịp spec tốn công |

## Consequences

- Phụ thuộc version FastMCP (pin version, nâng có kiểm soát).
- Tool contract phải ổn định — đổi schema tool là breaking change cho agent đã kết nối.
