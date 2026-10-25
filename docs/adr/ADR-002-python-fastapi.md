# ADR-002 — Python + FastAPI cho orchestration

**Status:** Accepted · **Date:** 2025 · **Liên quan:** [docs/02 §2](../02-system-architecture.md), [docs/06](../06-api-spec.md)

## Context

Lớp orchestration mỏng (~2–3k LOC) cần async I/O dày đặc: fan-out search, scrape song song (semaphore 4–6), SSE stream, gọi LLM. Hệ sinh thái "gã khổng lồ" đã chọn phần lớn là Python: crawl4ai, trafilatura, FastMCP, SearXNG.

## Decision

**Python 3.12 + FastAPI + uvicorn**; `asyncio` cho fan-out; Pydantic v2 cho schema; SSE qua `StreamingResponse`.

## Rationale

- Cùng ngôn ngữ với crawl4ai/trafilatura/FastMCP → nhúng trafilatura in-process (50–200ms/trang tĩnh) không cần hop mạng.
- FastAPI native SSE + tự sinh OpenAPI khớp [docs/06](../06-api-spec.md).
- Workload I/O-bound nên GIL không phải vấn đề; CPU-bound (rerank int8) chạy trong worker riêng.

## Alternatives

| Phương án | Vì sao không chọn |
|---|---|
| TypeScript/Next.js (Perplexica, morphic) | Phải gọi crawl4ai qua HTTP (thêm hop), 2 codebase, mất in-process trafilatura |
| Go | Hệ sinh thái extraction/LLM yếu hơn nhiều; tốc độ không phải bottleneck (latency nằm ở search + LLM) |

## Consequences

- Cần quản lý dependency cẩn thận (crawl4ai kéo Playwright — chỉ cài trong image crawl4ai service, không vào image api).
- Type safety dựa vào Pydantic + mypy; chấp nhận đổi lại tốc độ phát triển và hệ sinh thái.
