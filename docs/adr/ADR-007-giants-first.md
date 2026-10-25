# ADR-007 — Chiến lược "Đứng trên vai người khổng lồ"

**Status:** Accepted · **Date:** 2025 · **Liên quan:** [docs/02 §0](../02-system-architecture.md), [docs/12 Phase 0](../12-roadmap-plan.md)

## Context

Các hệ thống tham chiếu (gpt-researcher, Perplexica, farfalle, morphic) đều tự viết tầng search/scraper. Bảo trì parser engine + scraper là gánh nặng lớn nhất của dự án search — và dự án này do 1 người maintain, trên máy 16GB.

## Decision

Đứng trên **4 giants**, chỉ viết orchestration mỏng ~2–3k LOC:

| Giant | Vai trò | Ta không viết gì |
|---|---|---|
| **SearXNG** | Meta-search core (fan-out 70+ engine, dedup, consensus) | Parser engine, chống block/captcha |
| **crawl4ai** | Extraction JS-heavy (Playwright/Chromium đóng gói sẵn) | Browser automation, render pipeline |
| **trafilatura** | Extraction tĩnh in-process | Boilerplate removal, readability |
| **FastMCP** | Interface cho AI Agent | Protocol MCP, JSON-RPC |

Ta viết: query understanding, planner/executor, SSRF guard, citation validation, SSE, cache, LLM Gateway, Engine Registry mỏng.

**Nguyên tắc:** engine upstream đổi cấu trúc → nâng image giant, orchestration không đổi. Phase 0 của roadmap là scaffold giants trước, code của ta sau.

## Rationale

- Giảm 70–80% code phải maintain so với tự viết retriever + scraper.
- $0 phí tầng search; cộng đồng giants sửa bug block/captcha liên tục.
- Mượn ý tưởng tốt từ hệ tham chiếu mà không fork: Engine Registry hot-config từ chen150450/multi-search-aggregator; planner/executor pattern từ gpt-researcher; SSE + Generative UI từ Perplexica/morphic.

## Alternatives

| Phương án | Vì sao không chọn |
|---|---|
| Fork gpt-researcher | Khó tùy biến sâu; kèm UI/agent không cần; lệch kiến trúc mong muốn |
| Viết từ đầu | Thời gian gấp 5–10 lần; gánh nặng bảo trì parser |
| Dùng SaaS (Perplexity API, Jina) | Phí theo query + lock-in; mất kiểm soát pipeline |

## Consequences

- Phụ thuộc upstream → giảm rủi ro bằng pin image version + smoke test khi nâng (monthly, [docs/08 §6](../08-deployment.md)).
- Một số tính năng giants không dùng → bỏ qua qua config, không fork.
