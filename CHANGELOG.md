# Changelog

Tất cả thay đổi đáng chú ý của MetaSearch được ghi ở đây.
Format theo [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
version tuân theo [SemVer](https://semver.org/).

## [0.4.0] — 2026-10-29 — Observability, operations & performance

### Added
- **Grafana + Prometheus thật**: `deploy/prometheus/` scrape config, `deploy/grafana/`
  auto-provisioning + dashboard 8 panel (readiness, RPS, 5xx %, cache-hit %,
  HTTP p50/p95, engine calls/latency, LLM tokens + cost) — bật qua
  `docker compose --profile observability up -d`, UI tại `:3001`.
- **Quản lý operations**: `GET /v1/ops/status` (tổng hợp health DB/Redis/SearXNG/
  crawl4ai + engine health + version) và `GET /v1/queries` (lịch sử query gần đây,
  dùng cho debug/eval).
- **Common error phía client**: `ApiError {code, status}` trong `frontend/lib/api.ts`
  map 1-1 với error model backend; SSE `error` event truyền cả code.
- **Performance harness**: `api/scripts/perf_load.py` (micro benchmarks + load test
  API thật với fake SearXNG, không cần Docker) và `deploy/k6/load-test.js`
  (thresholds theo KPI docs/12) cho stack compose.

### Fixed
- **DB-partition stall** (phát hiện bởi perf test): khi Postgres bị partition
  (connect bị DROP), engine-health write awaited trong critical path chờ asyncpg
  timeout 3s → bão hòa pool → pool timeout 30s, mất 132/200 requests. Mọi DB op
  của `DbEngineStore` giờ bounded `ENGINE_STORE_DB_TIMEOUT_S=0.5` (fallback
  in-memory), `pool_timeout=5s`, thêm `ENGINE_STORE_BACKEND=memory`.

## [0.3.1] — 2026-10-29 — Expert audit (3 pass)

### Fixed
- **CORS thiếu hoàn toàn** — Web UI browser không gọi được API cross-origin.
  Thêm `CORSMiddleware` + `CORS_ORIGINS` (verify live: preflight/allow/block).
- crawl4ai/jina response chưa cap → `_cap()` theo `max_scrape_bytes`.
- Answer cache key thiếu search params (k/categories/freshness) theo spec
  `an:{sha1(query+model+params)}`.
- Answer JSON path chậm ~3s khi DB chết → persist + semantic cache chạy trong
  1 background task có thứ tự (FK vẫn đúng).
- LLMGateway dùng lại shared HTTP pool; DB pool 5+5 → 10+10.
- Frontend SSE flush frame cuối; README `language` → `lang`.

### Added
- +23 tests (CORS, RobotsCache, page-cache roundtrip, negative-cache qua Redis
  flag, worker recovery probe, queries 400, cache-key semantics).

## [0.3.0] — 2026-10-27 — Hardening sớm + token counting

### Added
- **Pin-IP connector** (đẩy từ Phase 4 lên): mọi hop fetch scrape/robots connect
  thẳng vào IP đã validate qua SSRF guard, giữ Host header + TLS SNI
  (`sni_hostname`) → đóng cửa sổ DNS rebinding; redirect re-resolve + re-pin.
- **tiktoken token counting** (tuỳ chọn): `CONTEXT_TOKENIZER=tiktoken` +
  `pip install '.[tokens]'`; budget đổi tên `ANSWER_CONTEXT_BUDGET`; fallback
  words tự động khi offline.
- Integration tests gated `METASEARCH_INTEGRATION=1` (8 case cho compose stack).

## [0.2.0] — 2026-10-27 — Phase 1: MVP Search

### Added
- `POST /v1/search`, `POST /v1/answer` (JSON + SSE: status/engine_done/sources/
  token/citations/done), `POST /v1/scrape`, `GET /v1/engines`, CLI
  `metasearch search/answer`.
- SearXNG adapter (JSON, timeout 3s, retry 1, 403 non-retryable), engine registry,
  canonical URL dedup + consensus + diversity cap, scoring v1 (BM25-lite 6 tín hiệu),
  scraper trafilatura → crawl4ai → jina với SSRF guard v1 + robots.txt + 5MB cap,
  chunker 512/64 heading-aware, LLM gateway streaming (OpenAI-compatible +
  Anthropic) với citation validation + cost metering, best-effort persistence.

## [0.1.0] — 2026-10-26 — Phase 0: Scaffold (giants-first)

### Added
- Monorepo `api/` (FastAPI) + `frontend/` (Next.js 15) + `deploy/` + `docs/`
  (12 tài liệu thiết kế + 8 ADR).
- docker-compose (searxng, crawl4ai, postgres+pgvector, redis, api, worker,
  frontend) + SearXNG JSON config + `.env.example`.
- CI (lint + unit + security scan + frontend build), `/healthz`, `/readyz`,
  `/metrics`, schema v1 (12 bảng + pgvector, Alembic).

[Unreleased]: Phase 3 (Deep Research & MCP: planner/executor loop, `/v1/research`
async job, FastMCP 4 tools, auth API key + rate limit, vertical engines) và
Phase 4 (k6 run, OTel tracing, Langfuse, eval harness RAGAS).
