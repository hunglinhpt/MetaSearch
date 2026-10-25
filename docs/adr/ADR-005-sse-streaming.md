# ADR-005 — SSE cho streaming

**Status:** Accepted · **Date:** 2025 · **Liên quan:** [docs/06](../06-api-spec.md), [docs/02 §3](../02-system-architecture.md)

## Context

Quick answer cần stream token + progress events (`status`, `engine_done`, `sources`, `citations`, `done`, `error`). Client đa dạng: Web UI, MCP agent, curl.

## Decision

**SSE** (`text/event-stream`) qua FastAPI `StreamingResponse` cho `POST /v1/answer` (mode `stream`). REST JSON thuần cho `/v1/search`, `/v1/scrape`. Không dùng WebSocket ở v1.

## Rationale

- SSE là HTTP thuần: đi qua proxy/LB chuẩn, tự reconnect, đủ cho stream một chiều.
- Perplexica/morphic đều dùng SSE — pattern đã được chứng minh cho AI search.
- Đơn giản hoá auth (header chuẩn) so với WebSocket handshake.

## Alternatives

| Phương án | Vì sao không chọn |
|---|---|
| WebSocket | 2 chiều nhưng phức tạp auth/proxy; không cần 2 chiều ở v1 |
| Polling | Latency cao, tốn request, UX kém cho token stream |
| gRPC streaming | Client web khó, overkill |

## Consequences

- Chỉ server→client — đủ vì client không gửi gì giữa chừng (huỷ = đóng connection).
- Cần heartbeat 15s chống idle timeout của proxy; Redis pub/sub khi scale nhiều api instance ([docs/08 §5](../08-deployment.md)).
