# 12 — Roadmap & Implementation Plan

## 1. Tổng quan 5 phase / 8 tuần

| Phase | Tuần | Deliverable | Exit criteria |
|---|---|---|---|
| **0 — Scaffold (giants-first)** | 0 | Repo structure, CI, docker-compose chạy SearXNG+crawl4ai+PG+Redis (dựng giants trước), health endpoints | `docker compose up` → `/readyz` 200; `GET searxng/search?format=json` trả JSON |
| **1 — MVP Search** | 1–2 | `/v1/search` + `/v1/answer` (JSON+SSE), SearXNG adapter (1 call), merge+score, scrape trafilatura→crawl4ai, CLI | Quick answer p50 ≤ 8s, có citation |
| **2 — Quality & UI** | 3–4 | Reranker, cache 2 tầng, engine health + hot config, Web UI Next.js, SSE `engine_done` | Raw p95 ≤ 2.5s, cache hit ≥ 30%, UI demo được |
| **3 — Deep Research & MCP** | 5–6 | Deep research loop, `/v1/research`, MCP server 4 tools, auth + API keys, rate limit | Deep report 30–180s có 10+ citation; MCP chạy với Claude Desktop |
| **4 — Hardening** | 7–8 | SSRF suite, k6 load test, observability (OTel+Prometheus+Langfuse), eval harness, docs hoàn thiện | KPI đạt (bảng §3), security checklist pass |

## 2. Task breakdown chi tiết

### Phase 0 — Scaffold (tuần 0) — ✅ HOÀN TẤT
- [x] Monorepo: `api/` (FastAPI), `frontend/` (Next.js), `deploy/`, `docs/`
- [x] docker-compose (searxng + crawl4ai + pg + redis, mem_limits dev 16GB) + searxng settings (formats json) + .env.example
- [x] CI: lint + unit + security scan
- [x] `/healthz`, `/readyz`, cấu trúc settings (pydantic-settings)
- [x] Alembic init + schema v1 (bảng engines, queries, search_results)

**Trạng thái verify (local, không có Docker):**

| Hạng mục | Lệnh | Kết quả |
|---|---|---|
| Unit test | `pytest -q` | 23 passed |
| Lint | `ruff check .` | All checks passed |
| Format | `ruff format --check .` | 31 files already formatted |
| Security scan | `bandit -q -r app -x tests` | 0 issue (exit 0) |
| Frontend typecheck | `npm run typecheck` | exit 0 |
| Frontend build | `npm run build` | exit 0 (Next.js 15, 4 static pages) |

> Exit criteria `docker compose up` → `/readyz` 200 chưa verify được trên máy dev (không có Docker Desktop);
> sẽ được xác nhận qua CI và trên máy có Docker. Phần `GET searxng/search?format=json` đã cấu hình sẵn
> trong `deploy/searxng/settings.yml` (`formats: [html, json]`).

### Phase 1 — MVP (tuần 1–2) — ✅ HOÀN TẤT
- [x] SearXNG adapter (async httpx, timeout 3s, retry 1; 403 = config error không retry)
- [x] Engine registry (config từ DB + env) — Phase 1: env-based, interface sẵn sàng cắm DB ở Phase 2
- [x] Merger: canonicalize, dedupe, consensus, diversity cap
- [x] Scoring v1: keyword (BM25-lite prefix-match) + consensus + engine_weight + freshness + snippet_quality
- [x] Scraper: trafilatura in-process → crawl4ai service fallback → jina reader (nếu có key), SSRF guard v1 (scheme/userinfo/DNS/private-IP/IPv4-mapped-IPv6), robots.txt, cap 5MB, redirect re-check mỗi hop
- [x] Chunking 512/64 heading-aware + LLM answer với citation prompt + post-validate citations
- [x] `/v1/search`, `/v1/answer` (JSON + SSE: status/engine_done/sources/token/citations/done), error model RFC 7807-ish
- [x] CLI: `metasearch search "query"`, `metasearch answer "query" [--stream]` (gọi REST API)
- [x] Unit tests core modules

**Trạng thái verify (local, không có Docker):**

| Hạng mục | Lệnh | Kết quả |
|---|---|---|
| Unit test | `pytest -q` | 91 passed (từ 23 ở Phase 0) |
| Lint | `ruff check .` + `ruff format --check .` | All checks passed, 60 files formatted |
| Security scan | `bandit -q -r app -x tests` | 0 issue (exit 0) |
| Smoke test live | uvicorn + curl /healthz, /v1/meta, /v1/engines, /v1/search, /v1/answer SSE | Graceful degradation đúng: 503 `no_results` + `engines_status` khi SearXNG vắng mặt; SSE event `status`/`error` đúng format; error model chuẩn |

> Ghi chú triển khai: LLM gateway streaming qua httpx thuần (OpenAI-compatible + Anthropic), không thêm SDK; persist best-effort fire-and-forget (không bao giờ fail request); rerank heuristic-first để sẵn trong `scoring.heuristic_chunk_score`.

### Phase 2 — Quality & UI (tuần 3–4) — ✅ HOÀN TẤT
- [x] Rerank heuristic-first (0ms) + tuỳ chọn int8 ONNX (`RERANKER=onnx`, lazy-load, `RERANKER_ONNX_DIR`) + tuỳ chọn Jina API (`RERANKER=api`) + fallback giữ thứ tự heuristic khi lỗi/thiếu dependency
- [x] Cache L1 Redis (exact): `sr:` search 30m (news 15m), `an:` answer 2h, `pg:` page 12h + L2 semantic (pgvector, cosine > 0.92, chỉ answer, replay có citation) + negative cache Redis (fallback in-memory khi Redis chết)
- [x] Engine health tracking (window/phút) + auto-disable sau 3 lỗi liên tiếp + recovery probe mỗi 5 phút (worker) + `PATCH /v1/engines/{id}` hot config (Postgres-backed, fallback in-memory khi DB chết) + `GET /v1/engines/health`
- [x] SSE events đầy đủ (`status`, `engine_done`, `sources`, `token`, `citations`, `done`) — có từ Phase 1, kiểm chứng thêm event `cached`
- [x] Web UI Next.js: search box, 2 mode (Trả lời AI stream / Tìm kiếm raw), streaming answer, sources panel, citations hover + click, engine chips, cached badge
- [x] SimHash near-duplicate filter (64-bit, Hamming ≤ 3, fold engines signals vào bản chính)
- [x] Tavily adapter (fallback khi SearXNG bị block — recommendation) + `/v1/queries/{id}` debug endpoint
- [x] Integration tests với compose — script chuẩn bị sẵn (`METASEARCH_INTEGRATION=1 pytest tests/integration`), chạy trên máy có Docker

**Trạng thái verify (local, không có Docker):**

| Hạng mục | Lệnh | Kết quả |
|---|---|---|
| Unit test | `pytest -q` | 138 passed (từ 91 ở Phase 1) |
| Lint | `ruff check .` + `ruff format --check .` | All checks passed |
| Security scan | `bandit -q -r app -x tests` | 0 issue (exit 0) |
| Frontend | `npm run typecheck` + `npm run build` | exit 0, First Load JS 105 kB |
| Smoke test live | uvicorn + curl engines/PATCH/health/search | Hot config hiệu lực ngay (PATCH weight 1.5), health tracking ghi nhận failure thực (failures=1, success_rate=0), PATCH engine lạ → 404, DB chết → fallback in-memory vẫn chạy |

> Recommendation đã xử lý: Tavily adapter ✓ · negative cache Redis ✓ · SSE client dùng fetch-stream (EventSource không hỗ trợ POST) ✓ · compose env fix `NEXT_PUBLIC_API_BASE_URL` ✓.
> **Recommendation bổ sung (đã xử lý sau Phase 2):**
> - ✅ **Pin-IP connector** (đẩy từ Phase 4 lên sớm): `app/extraction/pinning.py` — mỗi hop fetch scrape/robots connect thẳng vào IP đã validate qua SSRF guard, giữ `Host` header + TLS SNI (`sni_hostname` extension của httpcore) → đóng cửa sổ DNS rebinding. Redirect hop nào cũng re-resolve + re-pin.
> - ✅ **tiktoken token counting** (tuỳ chọn): `app/llm/tokens.py` + `CONTEXT_TOKENIZER=tiktoken` + `pip install '.[tokens]'`; budget context đổi tên `ANSWER_CONTEXT_BUDGET` (đơn vị words hoặc tokens theo tokenizer); fallback words tự động khi thiếu package/offline.
> - ⏳ Verify Docker: máy dev không có Docker (`docker: command not found`); 8 integration tests sẵn sàng chạy khi có stack (`METASEARCH_INTEGRATION=1 pytest tests/integration`).
> **Audit chuyên sâu 3 góc nhìn (sau recommendation):**
> - 🔒 Security: **CORS thiếu hoàn toàn** (Web UI browser bị block cross-origin) → thêm `CORSMiddleware` + `CORS_ORIGINS`; crawl4ai/jina response chưa cap → `_cap()` theo `max_scrape_bytes`; robots fail-open verified an toàn (main fetch chặn riêng).
> - ⚡ Concurrency: answer JSON path delay ~3s khi DB chết (persist awaited vì FK) → persist + semantic-cache viết trong **1 background task có thứ tự** (FK đúng, client không chờ); LLMGateway dùng lại shared HTTP pool; DB pool 5+5 → 10+10 chống cạn khi fan-out + persist chạy đồng thời.
> - 📐 Correctness: answer cache key thiếu search params (spec: `an:{sha1(query+model+params)}`) → thêm k/categories/freshness; frontend SSE flush frame cuối; README fix `language` → `lang`.
> - Tests: 151 → **174 passed** (+23: CORS ×4, RobotsCache ×8, page-cache roundtrip, negative-cache qua Redis flag, worker probe ×4, queries 400, cache-key semantics ×4).

### Bổ sung sau audit: observability + common error + performance (không thuộc phase)

- **Grafana + Prometheus thật** (trước đây chỉ 2 service rỗng trong compose): `deploy/prometheus/prometheus.yml` scrape `api:8000/metrics`; `deploy/grafana/` provisioning datasource + dashboard 8 panel (readiness, RPS, error %, cache-hit % theo KPI ≥ 40, HTTP p50/p95 theo KPI search p95 ≤ 2.5s, engine calls/latency, LLM tokens + cost). Bật: `docker compose --profile observability up -d` → `localhost:3001`.
- **Common error phía client**: `frontend/lib/api.ts` thêm class `ApiError {code, status}` map 1-1 với error model backend (`core/errors.py`); `page.tsx` hiển thị `[code] message`; SSE `error` event truyền cả code.
- **Performance test thật** (`api/scripts/perf_load.py`, không cần Docker — fake SearXNG local + uvicorn in-process):

| Scenario | p50 | p95 | p99 | errors |
|---|---|---|---|---|
| Micro: merge+score 500 raws | 57.8ms | — | — | — |
| Micro: SimHash ×300 | 14.7ms | — | — | — |
| Micro: canonicalize ×5000 | 39.1ms | — | — | — |
| GET /healthz (c=20, n=400) | 69ms | 224ms | 618ms | 0 |
| POST /v1/search c=1 | **131ms** | 193ms | 199ms | 0 |
| POST /v1/search c=5 | 677ms | 724ms | 732ms | 0 |
| POST /v1/search c=25 | 3.5s* | 4.3s* | 5.2s* | 0 |

  \* c=25 bị giới hạn bởi fake ThreadingHTTPServer (~7 rps × 25 concurrent = 3.5s queue — client-side queuing, không phải app); SearXNG thật (Go) sẽ không có trần này. k6 script cho stack thật: `deploy/k6/load-test.js` (thresholds theo KPI docs/12 §3).

- **Performance test tìm ra 1 bug resilience thật**: Postgres partition (connect bị DROP, không refused) → `record_result` awaited trong critical path chờ asyncpg timeout 3s → pending connects bão hòa pool → **pool timeout 30s, p50 30s, 132/200 requests lỗi**. Fixes: (1) mọi DB op của `DbEngineStore` bounded bởi `ENGINE_STORE_DB_TIMEOUT_S=0.5` → fallback in-memory; (2) `pool_timeout=5` thay 30s; (3) setting `ENGINE_STORE_BACKEND=memory` cho dev/perf không DB. Sau fix: 0 errors, không còn treo.
- Repo hygiene: `git status` sạch, `.gitignore` phủ đủ (`__pycache__/`, `.next/`, `*.tsbuildinfo`, `.venv/`...), không artifact nào bị track.

### Phase 3 — Deep Research & MCP (tuần 5–6) — ✅ HOÀN TẤT (v0.5.0)
- [x] Planner (tách sub-queries, JSON schema, heuristic fallback khi LLM lỗi), Executor (thuần async: search+scrape+rerank → ContextPack), Synthesizer (report streaming có citation)
- [x] `/v1/research` async job (202 + in-memory job store, cap 5 job đồng thời) + `GET /v1/research/{id}` + `GET /v1/research/{id}/events` SSE (plan/iteration_start/engine_done/learnings/report_token/citations/done); report persist Answer(kind="research"); budget hard-cap (iterations/time/tokens, `truncated: true` khi dừng sớm)
- [x] LLM Gateway: `complete()` non-streaming + retry 3 lần exponential backoff + task routing (planner/extractor → quick model, report → report model) + cost metering per job
- [x] MCP server (docs/07, layer mỏng gọi REST): 4 tools `web_search` / `web_scrape` / `web_search_and_scrape` / `deep_research` (async, trả research_id + poll_hint) — stdio (`python -m app.mcp`) + streamable HTTP (mount `/mcp` khi `MCP_ENABLED=true`); optional dep `.[mcp]`; tool logic tách `app/mcp/client.py` test được không cần SDK
- [x] Auth: API key `msk_*` (sha256 hash + prefix, cache 60s, fail-closed khi DB chết), sliding-window rate limit Redis ZSET (429 + Retry-After + X-RateLimit-*), role admin/user, CLI `metasearch keys create/list`, migration 0003; `AUTH_ENABLED=true` để bật (mặc định off cho local + Web UI)
- [x] Vertical engines: arXiv (Atom API, defusedxml) + Wikipedia (MediaWiki, theo lang) — chỉ vào fan-out khi request chứa category tương ứng; `engines[]` explicit override

**Trạng thái verify (local, không có Docker):**

| Hạng mục | Lệnh | Kết quả |
|---|---|---|
| Unit test | `pytest -q` | 215 passed + 8 integration (từ 182) |
| Lint | `ruff check` + `ruff format --check` | All clean |
| Security | `bandit -q -r app -x tests` | 0 issue (defusedxml cho arXiv Atom) |
| Smoke live | uvicorn: auth bật → 401 chuẩn; research 202 → job running → SSE events; LLM none → planner fallback + error event đúng format | ✓ |

### Phase 4 — Hardening & AI SOTA Upgrades — ✅ v0.7.0 hoàn tất
- [x] Eval harness: pi/evals/golden.json (mở rộng 30 query đa miền EN+VI: developer, academic, social, reference, security) + pi/scripts/eval_run.py (hit@5, answer_rate, citation_accuracy)
- [x] Dream-RSI Offline Replay Simulator (pp/orchestration/simulator.py + eval_run.py --replay-from-db): mô phỏng replay world model không tốn chi phí API/mạng ngoài từ lịch sử DB
- [x] Harness² Domain Scaffolding (pp/orchestration/research.py): adaptive domain-specific research profiles (developer, cademic, comparative, general) + contrastive gap follow-ups
- [x] 4/4 Vertical engines hoàn tất: arXiv, Wikipedia, StackOverflow (category: developer), Reddit (category: social)
- [x] Durable research job recovery: tự động nạp lại kết quả hoàn thành từ DB khi server restart
- [x] MCP Ecosystem Support (OpenAI Codex, Claude Code, Cursor, Windsurf, Claude Desktop)
- [x] Langfuse LLM tracing (optional, httpx thuần không SDK): LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY → trace per LLM call fire-and-forget
- [x] OTel tracing (optional .[otel]): FastAPI + httpx instrumentation, OTEL_ENABLED=true + OTEL_* env
- [x] k6 script chuẩn bị sẵn (deploy/k6/load-test.js, thresholds theo KPI) — **cần chạy trên máy có Docker** (docker compose up + k6 run)
- [ ] k6 load test + tuning thực tế — chờ máy có Docker (chạy METASEARCH_INTEGRATION=1 pytest tests/integration + k6 run deploy/k6/load-test.js)
- [ ] Backup job + runbook vận hành — todo nhỏ còn lại
- [ ] SSRF suite còn lại: pin-IP cho crawl4ai/jina service tier (direct fetch đã pin từ Phase 2)

## 3. KPI nghiệm thu (từ [01](01-product-requirements.md))

| KPI | Target | Đo bằng |
|---|---|---|
| Quick answer p50 / p95 | ≤ 6s / ≤ 12s | Prometheus histogram |
| Raw search p95 | ≤ 2.5s | Prometheus |
| Citation accuracy | ≥ 90% | Eval harness |
| Faithfulness (RAGAS) | ≥ 0.85 | Eval harness |
| Cache hit rate | ≥ 40% | Counter |
| Engine success rate | ≥ 95% | Engine health |
| Cost / quick query | ≤ $0.005 | LLM metering |
| RAM stack (dev 16GB) | ≤ 4GB trong WSL2 6GB | `docker stats` |

## 4. Rủi ro thực thi

| Rủi ro | Giảm thiểu |
|---|---|
| SearXNG bị Google block (rate limit upstream) | limiter off nội bộ + engine phụ Tavily/Serper + cache |
| **RAM 16GB không đủ cho stack** | cap WSL2 6GB + mem_limits từng service (tổng ~4GB); không chạy model lớn local ([ADR-008](adr/ADR-008-hardware-inference.md)); crawl4ai concurrency 2 |
| Reranker chậm trên CPU | heuristic-first mặc định (0ms); int8 ONNX tuỳ chọn; API rerank nếu cần |
| Deep research vượt budget | hard cap time/tokens/iterations; checkpoint mỗi iteration |
| Scope creep UI | UI Phase 2 chỉ 3 màn hình: search, answer, sources |
| Một mình maintain | giants-first: tự viết ít code nhất có thể; docs đầy đủ, mọi module có test |
