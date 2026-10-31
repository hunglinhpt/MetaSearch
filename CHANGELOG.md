# Changelog

Tất cả thay đổi đáng chú ý của MetaSearch được ghi ở đây.
Format theo [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
version tuân theo [SemVer](https://semver.org/).

## [0.6.0] — 2026-10-31 — Lite profile: chạy không Docker trên máy yếu

### Added
- **DuckDuckGo adapter (keyless)**: general search không cần SearXNG/API key —
  unwrap link `/l/?uddg=`, parse date từ snippet, throttle 1.5s, phát hiện
  bot-challenge (403/429/202 + anomaly page).
- **SQLite persistence (Lite profile)**: models dùng dialect variants
  (`sa.Uuid` / JSON→JSONB / JSON→ARRAY / JSON→Vector) — cùng một bộ models
  chạy trên Postgres+pgvector lẫn SQLite (WAL, auto-schema lúc boot,
  `aiosqlite`); engine store upsert dialect-aware; auth/keys/query history
  hoạt động đầy đủ trên SQLite.
- **In-process fallbacks giữ chất lượng khi không Redis**: L1 cache dùng
  bounded TTL cache (512 entries) thay vì cache miss; rate limit dùng local
  sliding window thay vì bỏ limiter — Lite profile vẫn được bảo vệ + cache.
- **Jina Reader keyless** (~20 rpm) làm tier JS-rendering thay crawl4ai;
  `JINA_KEYLESS=true` mặc định, có key thì rate cao hơn.
- `deploy/.env.lite.example` + docs/08 §Lite + README Quick Start 2 profile.
- RAM đo thực tế Lite mode: **API ~100MB** (compose stack ~4GB).

## [0.5.0] — 2026-10-31 — Phase 3: Deep Research & MCP + Phase 4 lõi

### Added
- **Deep Research (FR-9)**: planner LLM (JSON sub-queries, heuristic fallback)
  -> executor (search+scrape+rerank -> ContextPack) -> extractor (learnings +
  gaps) -> vòng lặp trong budget -> synthesizer report streaming có `[n]`
  citations. `POST /v1/research` (202, cap 5 job) + `GET /v1/research/{id}` +
  `GET /v1/research/{id}/events` (SSE 7 loại event). Report persist
  Answer(kind="research"); budget vượt -> `truncated: true`.
- **MCP server (FR-10)**: 4 tools `web_search` / `web_scrape` /
  `web_search_and_scrape` / `deep_research` theo docs/07 (markdown output,
  timeout + error thân thiện, cap 12k chars). stdio: `python -m app.mcp`;
  streamable HTTP: mount `/mcp` khi `MCP_ENABLED=true`. Optional dep `.[mcp]`.
- **Auth + rate limit (FR-14)**: API key `msk_*` (sha256 + prefix + cache 60s,
  fail-closed khi DB chết), sliding-window Redis ZSET (429 + Retry-After +
  X-RateLimit-*), role admin/user, CLI `metasearch keys create/list`,
  migration 0003. `AUTH_ENABLED=true` để bật (mặc định off cho local/Web UI).
- **Vertical engines (FR-3)**: arXiv (Atom, defusedxml) + Wikipedia (MediaWiki,
  theo lang) — vào fan-out theo category; `engines[]` explicit override.
- **Serper + Brave adapters** (FR-2 hoàn chỉnh — 5/5 engines có adapter thật).
- **Phase 4 lõi**: eval harness (`evals/golden.json` + `scripts/eval_run.py`
  — hit@5, answer_rate, citation_accuracy), Langfuse LLM tracing (httpx thuần,
  optional), OTel FastAPI+httpx instrumentation (optional `.[otel]`).

### Fixed
- arXiv Atom parsing qua `defusedxml` (bandit B314/B405 — untrusted XML).
- Research plan/learnings event bị trùng lặp trong SSE (callback + yield).

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
