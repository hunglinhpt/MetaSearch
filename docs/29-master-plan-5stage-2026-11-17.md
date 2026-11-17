# docs/29 — Master Engineering Plan: 5-Stage Transformation (2026-11-17)

Stage 1 = 3 auditor song song (security/zero-trust · algorithms/concurrency ·
docs-parity/devsecops), bằng chứng `file:line` thật. Stage 2 = benchmark
[Perplexica](https://github.com/ItzCrazyKns/Perplexica),
[Morphic](https://github.com/miurla/morphic), digest pipelines
([awesome-ai-news](https://github.com/taielab/awesome-ai-news),
[Desearch digest](https://www.desearch.ai/use-cases/daily-news-digest),
[KNIME GenAI digest](https://knime.com/blog/how-create-and-send-daily-news-digest-genai-and-knime))
— pattern chung: **metasearch → dedup canonical → synthesis có citation đánh
số thật → digest layer định kỳ**. Stage 4 = cuốn chiếu item-by-item kèm
micro-audit. Stage 5 = 3-pass convergence.

## Anchor sản phẩm (yêu cầu trực tiếp của chủ)

Bản tin (AutomatedScanner) **sơ sài + fabricated**: `scheduler.py:176-348`
hardcode bảng/nhận định không phụ thuộc results (bảng VN-Index, "P/E vùng
hấp dẫn", "Cursor, Claude 3.5...") — vi phạm quy tắc no-fabricate; `scan_all`
(scheduler.py:474) tuần tự 6 topic → 6 post rời rạc, **không có bản tin chung
toàn đợt**; không dedup cross-engine; citation placeholder
`https://metasearch.ai`; citation `[n]` gắn mù theo vị trí.

## [P0] Critical — integrity & quickstart

| # | Item | Files | Spec |
|---|---|---|---|
| P0-1 | **Scanner integrity + Unified Run Digest (bản tin chung)** | `orchestration/scheduler.py` | (a) dedup cross-engine bằng `canonicalize` + simhash trước đánh số citation; (b) synthesis **extractive** — mọi câu/mục nội dung lấy từ title+snippet THẬT, xóa toàn bộ template fabricated (bảng VN-Index, nhận định không nguồn); (c) citation chỉ từ URL thật, bỏ placeholder; (d) escape markdown chars trong title/snippet (AppSec F7); (e) `scan_all` tạo **1 post `run_digest`** tổng hợp toàn đợt: header thống kê chạy (topic, engine ok/fail, tổng tin sau dedup, as_of), section per topic top-N tin thật, cross-topic hot sources, citation hợp nhất; (f) `scan_all` parallel bounded `Semaphore(2)` + per-topic timeout + single-flight lock + background loop skip topic có `last_run` mới (F#2, #23 round B); (g) escape + structure tests |
| P0-2 | **Quickstart thật sự chạy được** | `README.md` | `cp deploy/.env.example deploy/.env` (compose đọc .env trong dir của compose — bằng chứng nightly.yml:26-33); thêm bước `keys create` + Bearer vào curl (compose ép AUTH_ENABLED=true); fix PATH trap `.venv/Scripts/metasearch` |
| P0-3 | **SECURITY.md** | root | File thiếu hoàn toàn — thêm disclosure policy, supported versions, contact |

## [P1] Core hardening

| # | Item | Files | Spec |
|---|---|---|---|
| P1-1 | research.py khôi phục SEC-07 delimiters (AppSec F3) | `orchestration/research.py` | `_render_pack` bọc từng source trong delimiter ngẫu nhiên như `render_context` |
| P1-2 | spaces graph/query delimiter (F6) | `api/v1/spaces.py` | context KG bọc delimiter trước khi vào LLM |
| P1-3 | answer cache key thiếu `domain` → cross-domain replay (B#5) | `orchestration/pipeline.py` | `_cache_hit`/`_cache_store` truyền `domain=domain` |
| P1-4 | persist: embeddings NHẬP SQLite lock + bulk chunk insert (B#4, B#10) | `orchestration/persist.py` | tính embeddings trước khi lấy lock; 1 executemany thay N round-trip |
| P1-5 | SSRF TOCTOU crawl4ai (F2) | `extraction/scraper.py` | SHIPPED dạng: re-resolve ngay trước dispatch + re-check final_url sau dispatch (fail-closed khi DNS flip về internal). So-sánh IP-set 2 lần resolve KHÔNG ship (P3-13) — residual: flip giữa 2 public IP vẫn pass; documented. |
| P1-6 | Exception leak 3 surfaces (F8) | `api/v1/answer.py`, `orchestration/research_jobs.py`, `api/v1/llm.py` | trả error code + type name; chi tiết chỉ vào log |
| P1-7 | Podcast owner-prefix comment sai 64/128 bit (F10) | `api/v1/podcast.py` | sửa comment (giữ 16 hex — đủ thực tế) |

## [P2] Standardization & doc sync

| # | Item | Files |
|---|---|---|
| P2-1 | Env example drift: thêm `TELEGRAM_BOT_TOKEN/CHAT_ID, WEBHOOK_URL, DISCORD_WEBHOOK_URL, FA_FIREANT_TOKEN`; đổi `RERANKER_ONNX_DIR`→`ONNX_RERANKER_DIR` (phantom); thêm biến đọc-mà-không-doc (`SCHEDULER_*`, `MAX_BODY_BYTES`, `RATE_LIMIT_RPM`, `DEV_ANON_ADMIN`, `RETENTION_*`, `MCP_ENABLED`, `OTEL_ENABLED`, `SUPERAGENT_ENABLED`) | `deploy/.env.example`, `docs/12-roadmap-plan.md` |
| P2-2 | Sync số liệu thật: 1087 backend / 107 frontend tests; MCP **9** tools; skills **10**; adapters **26**; golden **46**; version 3.2.0 | `README.md` |
| P2-3 | Repo map + status update | `AGENTS.md`, `CHANGELOG.md`, `docs/12`, `docs/15`, `docs/27`, `docs/29` — ✅ COMPLETED |
| P2-4 | Streaming markdown hardening | `frontend/components/{AnswerMarkdown,CodeBlock,MathFormula,MermaidDiagram}.tsx`, `frontend/lib/inlineMd.ts` — state reset giữa render/fail, `MAX_FAILED_EMPHASIS=64` chống `O(n^2)`, `trust: false` tường minh, `useEffect` StrictMode gate |
| P2-5 | Frontend container healthcheck | `frontend/Dockerfile`, `deploy/docker-compose.yml` — Node `fetch` `HEALTHCHECK` |

## [P3] High-ROI upgrades (chọn lọc, ROI/risk cao)

| # | Item | Files |
|---|---|---|
| P3-1 | Gateway 429 backoff exponential + jitter (B#13); SearXNG retry sleep (B#14) | `llm/gateway.py`, `retrieval/adapters/searxng.py` |
| P3-2 | `sha1_key` length-prefix chống collision (B#21); guard `canonicalize` trong research (B#16); hybrid `_cosine` precompute norm (B#24); `apply_signals` hoist tokenize (B#12) | `cache.py`, `research.py`, `hybrid.py`, `scoring.py` |
| P3-3 | Frontend: badge `🌐 Bản tin chung` + xếp-first cho topic `run_digest` — ✅ COMPLETED | `frontend/app/page.tsx`, `frontend/app/globals.css` |

## DEFER — Trạng thái hoàn thiện (2026-11-17)

- ✅ **DEFER-A / B / C (đã hoàn tất trong chuỗi fix sau crash quota GLM + 3 vòng audit):**
  - `core/auth.py`: thêm `/v1/hybrid` `POST` cost `2` (`AppSec F5`), cắt tỉa expired windows trước LRU trong `_local_rate_limit` (`B#20`).
  - `core/admission.py`: `get_key_semaphore` LRU chuẩn (move-to-end khi hit, chỉ evict slot nhàn rỗi `_value >= limit`).
  - `api/v1/market.py`: `_csv_safe_cell` chống CSV formula injection (`=`, `+`, `-`, `@`, `\t`, `\r`, giữ nguyên số âm); `asyncio.wait` bảo toàn batch hoàn thành khi timeout fan-out; gắn cờ `QUANT_FETCH_SKIPPED_BUDGET` khi bỏ qua live quant do cạn budget.
  - `orchestration/engine_store.py`: khóa `sqlite_write_lock()` & giá trị trả về `consecutive != prev_consecutive` trong `_record_db`, invalidates `_cache` + tăng `_cache_gen` khi `update()` fall back in-memory, giữ `record_result` in-memory (0-SQL trên search hot path).
  - `cache.py`: `_LocalTTLCache` dùng min-heap `_expiry_heap` + rebuild có cận trên.
  - `api/v1/podcast.py`: miễn `pending`/`running` khi `_prune_jobs`, chạy song song `_gather_sections` qua `Semaphore(3)` (`B#18`, `B#25`).
  - `api/v1/spaces.py`: `_graph_inflight` chống evict build đang chạy (`B#17`), bọc `query_graph_endpoint` bằng delimiter ngẫu nhiên SEC-07 (`P1-2`).
  - `llm/council.py` & `orchestration/pipeline.py`: tổng hợp `usage` thật xuyên suốt 3 pha council (`B#26`), `asyncio.gather` cho `expansion_variants` (`B#8`), offload `compress_chunks`/`build_context` của `/v1/context` qua `run_cpu` (`B#22`).
  - `retrieval/fa_sources/aggregator.py`: `fetch_ohlcv` short-circuit tuần tự theo priority (`B#15`).
  - **Regression & 3-round convergence fixes:** sửa tái-chèn trùng chunk khi re-scrape trong `orchestration/persist.py`, `sqlite_write_lock()` + bỏ double-dedup + escape `\`/`|` trong `orchestration/scheduler.py`, sửa lỗi `None` crash trong `_quant_only_payload` & giữ quant telemetry khi chỉ có tin đồn / nâng `S-Score >= 2.0` từ `ELEVATED` lên `VIRAL_SURGE` & lọc URL chỉ nhận `http(s)` trong `retrieval/sentiment.py` + `schemas.py`.
- ✅ **Hoàn tất trong v3.3.0 (bổ sung từ backlog):**
  - Opt-in guarded executive brief cho `run_digest` (`SCHEDULER_LLM_SYNTHESIS_ENABLED=false` mặc định, SEC-07 random delimiters, kiểm `validate_citations` + `prose_only >= 15`, timeout `12s` fail-open bảo toàn bản tin extractive; test ở `api/tests/test_proposals_wave.py`).

## Verification criteria (Stage 5)

- Pass 1: adversarial review mới trên toàn bộ diff của Stage 4 (fresh agent),
  mọi finding fix ngay.
- Pass 2: đo/đối chiếu — số test, key building, log format, doc parity
  (env-var lists khớp 100%).
- Pass 3: full gates từ trạng thái sạch: `pytest` (số đếm thật),
  ruff format+check, mypy, bandit, vitest, tsc, build, build:export,
  npm audit; cert 0 P0/P1 còn lại.
