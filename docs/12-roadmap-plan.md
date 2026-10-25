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

### Phase 0 — Scaffold (tuần 0)
- [ ] Monorepo: `api/` (FastAPI), `frontend/` (Next.js), `deploy/`, `docs/`
- [ ] docker-compose (searxng + crawl4ai + pg + redis, mem_limits dev 16GB) + searxng settings (formats json) + .env.example
- [ ] CI: lint + unit + security scan
- [ ] `/healthz`, `/readyz`, cấu trúc settings (pydantic-settings)
- [ ] Alembic init + schema v1 (bảng engines, queries, search_results)

### Phase 1 — MVP (tuần 1–2)
- [ ] SearXNG adapter (async httpx, timeout 3s, retry 1)
- [ ] Engine registry (config từ DB + env)
- [ ] Merger: canonicalize, dedupe, consensus, diversity cap
- [ ] Scoring v1: keyword + consensus + engine_weight
- [ ] Scraper: trafilatura in-process → crawl4ai service fallback, SSRF guard v1
- [ ] Chunking 512/64 + LLM answer với citation prompt
- [ ] `/v1/search`, `/v1/answer` (JSON + SSE), error model
- [ ] CLI: `metasearch search "query"`, `metasearch answer "query"`
- [ ] Unit tests core modules

### Phase 2 — Quality & UI (tuần 3–4)
- [ ] Rerank heuristic-first (0ms) + tuỳ chọn int8 ONNX ~300MB (`RERANKER=onnx`) + fallback giữ thứ tự heuristic
- [ ] Cache L1 Redis (exact) + L2 semantic (pgvector, threshold 0.92) + negative cache
- [ ] Engine health tracking + auto-disable + PATCH hot config
- [ ] SSE events đầy đủ (`engine_done`, `sources`, `token`, `citations`, `done`)
- [ ] Web UI: search box, streaming answer, sources panel, citations hover
- [ ] SimHash near-duplicate filter
- [ ] Integration tests với compose

### Phase 3 — Deep Research & MCP (tuần 5–6)
- [ ] Planner (tách sub-queries), Executor (loop), Synthesizer (report có citation)
- [ ] `/v1/research` async job + progress SSE
- [ ] LLM Gateway: OpenAI/Anthropic routing (Ollama 3B chỉ experimental offline) + fallback + cost metering
- [ ] MCP server (FastMCP): 4 tools + stdio/HTTP transport
- [ ] Auth: API keys (hash), rate limit Redis, admin role
- [ ] Vertical engines: arXiv, Wikipedia (nếu thời gian)

### Phase 4 — Hardening (tuần 7–8)
- [ ] SSRF suite đầy đủ + pin-IP connector + redirect control
- [ ] k6 load test + tuning (connection pool, semaphore)
- [ ] OTel tracing per stage + Prometheus metrics + Grafana dashboard
- [ ] Langfuse LLM tracing
- [ ] Eval harness: golden set 50 query, RAGAS, citation accuracy, nightly job
- [ ] Docs hoàn thiện + runbook + backup job

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
