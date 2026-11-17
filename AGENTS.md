# AGENTS.md — Hướng dẫn làm việc cho AI Agent trên repo này

Tài liệu này cho agent (và người mới) bức tranh đúng nhất về repo: đang ở đâu,
quy ước gì, làm việc tiếp từ đâu. Cập nhật tài liệu này khi phase thay đổi.

## 1. MetaSearch là gì?

**AI meta-search & deep research engine**: SearXNG (70+ engines) → crawl &
extract (trafilatura → crawl4ai → jina) → dedup/consensus/rerank → synthesize
có citation `[n]`. Đóng gói thành REST API + SSE + MCP Server + Web UI.

Chiến lược **giants-first** (ADR-007): tự viết tối thiểu (orchestration ~3k LOC),
đứng trên SearXNG/crawl4ai/trafilatura/FastMCP. Engine đổi cấu trúc → nâng image,
orchestration không đổi.

## 2. Trạng thái hiện tại (cập nhật: 2026-11-17 — v3.3.0: docs/27 Live VIX & Simplize Quant, docs/28 Rich-Content Web UI, docs/29 Unified Extractive Digest & DEFER-A/B/C, Full-App `mypy`, Opt-In Executive Digest & UI Parity)

- ✅ **v3.3.0 Convergence & 3-Round Adversarial Audit (`tests/test_proposals_wave.py`, `tests/test_defer_audit_fixes.py`)**:
  - **Full-app `mypy` (`api/pyproject.toml`)**: mở rộng phạm vi `[tool.mypy].files` từ `app/core`, `app/api`, `app/llm` sang toàn bộ `app/` (159 file), xử lý sạch toàn bộ lỗi annotation ở `app/orchestration/`, `app/retrieval/`, `app/mcp/client.py`, `app/config.py`; bổ sung test nâng độ phủ cho `site.py`, `hybrid.py`, `worker.py`, `tracing.py`, `podcast.py` personal, `scheduler.py`, `sentiment_quant.py` và `market.py`.
  - **Live Quant Proxies & Simplize Fallback (`sentiment_quant.py`, `market.py`)**: bổ sung `fetch_live_market_proxies` (`^VIX` + `^VIX3M` không cần key cho mã quốc tế khi `fetch_market_data=true`, `_merge_live_market_proxies` không ghi đè tham số caller), Simplize fallback khi `fa_aggregator` thiếu chỉ số VN, chặn ký tự symbol không hợp lệ qua `ensure_symbol`, và sửa `parse_yahoo_chart_bars` cho index thiếu `volume` / giá trị non-finite.
  - **Opt-In Executive Synthesis cho `run_digest` (`scheduler.py`, `deps.py`, `config.py`, `deploy/.env.example`)**: `SCHEDULER_LLM_SYNTHESIS_ENABLED=false` mặc định; khi bật, `_synthesize_executive_brief` tóm tắt điều hành trong rào ngẫu nhiên SEC-07, kiểm `validate_citations` + `prose_only >= 15`, timeout `12s` fail-open giữ nguyên bản tin extractive.
  - **Web UI Parity (`frontend/`)**: thêm tab `📊 Tâm lý & Dòng tiền` (`SentimentPanel.tsx`), tab `🗂️ Spaces & KG` (`SpacesPanel.tsx` với Spaces CRUD, Crawl, async KG build + bounded poll, KG query và `POST /v1/hybrid`), nút chuyển nhanh từ `ComparePanel.tsx`, và nút `"🎧 Podcast Watchlist"` (`/v1/podcast/personal*`) trong `NewsPanel.tsx`, kèm `AbortController`/generation guard và `api.test.ts` assertions.

- ✅ **Hậu-v3.2.0 Convergence (docs/28 & docs/29 + DEFER-A/B/C)** — chi tiết ở [CHANGELOG.md](CHANGELOG.md), [docs/28-chat-demo-evaluation-and-integration.md](docs/28-chat-demo-evaluation-and-integration.md) và [docs/29-master-plan-5stage-2026-11-17.md](docs/29-master-plan-5stage-2026-11-17.md):
  - **Streaming Rich-Content Web UI (`docs/28`, `frontend/`)**: `AnswerMarkdown.tsx`, `CodeBlock.tsx` (lazy Prism), `MathFormula.tsx` (KaTeX `trust: false`), `MermaidDiagram.tsx` (serial strict sanitize + state reset), `inlineMd.ts` (`MAX_FAILED_EMPHASIS=64` chống `O(n^2)`), StrictMode-pure `useEffect` pacing gate, badge/sort-first `run_digest` (`🌐 Bản tin chung`) trên `page.tsx`, và `HEALTHCHECK` trong `frontend/Dockerfile` / `deploy/docker-compose.yml`.
  - **Unified Extractive Digest & Hardening (`docs/29`)**: `AutomatedScanner` (`scheduler.py`) viết lại extractive thuần từ kết quả thật, `run_digest` tổng hợp toàn đợt, `sqlite_write_lock()`, hoàn tất toàn bộ DEFER-A/B/C (`admission` true LRU, `auth` `/v1/hybrid` cost & expired-first window prune, `market` CSV escape / `asyncio.wait` partial batch / `QUANT_FETCH_SKIPPED_BUDGET`, `engine_store` `_record_db` lock + `_cache_gen` & fallback cache invalidation + 0-SQL hot path, `cache` min-heap TTL, `podcast`/`spaces` active-job LRU guard + SEC-07 delimiters, `council` real `usage`, `pipeline` `expansion` gather & `run_cpu` offload, `persist` chống trùng chunk khi re-scrape, `sentiment` `None`-safe `_quant_only_payload` / rumor+quant / `S-Score >= 2.0` / `http(s)` driver URL, `FA` `fetch_ohlcv` short-circuit).

- ✅ **v3.2.0 4-Repo AI Infra Integration & 5-pass hardening (docs/25)** — chi tiết ở [CHANGELOG.md](CHANGELOG.md) và [docs/25-llm-integration-evaluation.md](docs/25-llm-integration-evaluation.md):
  - **OmniRoute-inspired validation & router** (`llm/key_validator.py`, `llm/router.py`): 17 `KEY_FORMAT_SPECS`, live probe SSRF-safe qua `get_http_client("internal")` (`asyncio.Semaphore(6)`), `scoped_runtime_override`, `ProviderHealthTracker` + EWMA + `LLM_ALLOW_KEYLESS_FALLBACK=false`, API `POST /v1/llm/validate` (`require_admin`) & `GET /v1/llm/health?task=answer|report`.
  - **Headroom-inspired compression** (`llm/compressor.py`): `ContentRouter` + `SmartCrusher` + verbatim `SearchCompressor` (4 profiles: `balanced`, `preserve_numbers`, `preserve_code`, `compact`) + `CacheAligner` (bọc recalled memory trong `<<<PRIOR_MEMORY_{tag}>>>`), hook vào `pipeline.py` (`CONTEXT_COMPRESSION_ENABLED`, `CONTEXT_COMPRESSION_MIN_CHUNKS=2`) & `/v1/context` `include=["compression"]`.
  - **One-Skill Orchestrator** (`orchestration/skill_orchestrator.py`, `api/v1/skills.py`): 10 skills (gồm `omni_channel_equity_sentiment`) + `TaskObserver` bounded telemetry, `GET /v1/skills`, `GET /v1/ops/skills`, `/v1/context` `include=["skill"]`.
  - **Omni-Channel Bilingual Equity Sentiment Engine (VI / EN)** (`retrieval/sentiment.py`, `retrieval/sentiment_quant.py`, `api/v1/market.py`, `mcp/server.py`, [docs/27-sentiment-measurement-standards-and-checklist.md](docs/27-sentiment-measurement-standards-and-checklist.md)): fan-out 5 lớp + cổng định lượng mở rộng (`fiintrade.vn`, `wichart.vn`, `simplize.vn`, `cboe.com`, `finra.org`, `aaii.com`, `naaim.org`, `finviz.com`, `barchart.com`, `tipranks.com`, `whalewisdom.com`, `quiverquant.com`, `unusualwhales.com`, `swaggystocks.com`, `apewisdom.io`, `sentimentrader.com`) hoặc bypass khi có `retrieved_documents`; chống trùng mã đa thị trường (`VNM`, `GAS`, `SSI`, `FPT`, `POW`), phát hiện seeding/lùa gà (`COORDINATED_SOCIAL_SEEDING`) & tin đồn chưa kiểm chứng (`UNVERIFIED_RUMOR_SPIKE`), bộ lọc phủ định song ngữ (`_is_negated_match`), trọng số `Tier_Weight × Time_Decay` kết hợp **Loughran-McDonald sublinear TF-IDF**, **Tetlock Media Pessimism** & **Consensus Polarization**, phân loại 6 `market_regime`, và khối chuẩn đo lường `benchmark_standards` (CNN Fear & Greed `0..100` với MA125/52W H-L, Vietstock Market Psychology, SentimenTrader Smart/Dumb Money & Contrarian, BofA Bull&Bear analog, AAII/II/NAAIM, CBOE Put/Call/VIX-VIX3M/SKEW, Arms TRIN, McClellan RANA/NYMO, SqueezeMetrics DIX/GEX, FINRA Margin YoY, Wilder RSI-14, MFI-14, CMF-21, StockTwits S-Score, Finnhub Insider MSPR, tùy chọn `ohlcv_bars`, `market_proxies` & `fetch_market_data=true` không bịa số, cùng `nlp_telemetry`, `quantitative_telemetry` và bảng kiểm 7 trụ cột `seven_pillar_checklist`), `POST /v1/market/sentiment`, MCP tool `equity_sentiment` & `tests/test_equity_sentiment.py`.
  - **Claude-Mem Progressive Disclosure** (`retrieval/memory.py`, `api/v1/memory.py`, `mcp/server.py`): `kind="observation"` theo `api_key_id`, recall bounded (`MEMORY_RECALL_MAX_TOKENS=250`), 3-layer REST (`/v1/memory/search`, `/v1/memory/timeline` anchor-indexed, `/v1/memory/observations`) + 3 MCP tools (`memory_search`, `memory_timeline`, `memory_get_observations`).
  - 5-pass audit fix xong toàn bộ lỗi logic, SSRF/pool, timeline >500 items, prompt-delimiter & DRY `scoped_runtime_override`; thêm `tests/test_phase4_ai_infra.py` (13 tests) & regen `openapi_v1.json`.
  - **Principal Architect Review & Boundary Hardening ([docs/26-principal-architect-review-2026-11-15.md](docs/26-principal-architect-review-2026-11-15.md))**: hoàn tất xử lý 22 finding (`F-01`..`F-22`) — bảng trọng số rate-limit `_STATIC_ROUTE_COSTS`, khai báo đủ tier `admit()`, bảo vệ `/v1/llm/council` & `/v1/spaces/{slug}/crawl`, `require_operator` cho các route `/v1/ops/*`, `/v1/llm/health`, `POST /v1/podcast/briefing`, chặn chunked body > `MAX_BODY_BYTES` (413), chống giả mạo loopback qua proxy (`is_direct_loopback`), ẩn chi tiết nội bộ `/readyz` với caller từ xa, khóa cận trên toàn bộ schema/query/path bằng 43 test chống tái phát trong `tests/test_w5_guardrails.py`, mở rộng `mypy` sang `app/llm`.

- ✅ **v3.1.0 Lite Web UI + job runner không popup (docs/08 §Lite, docs/24 §8)** — chi tiết ở [CHANGELOG.md](CHANGELOG.md):
  - Web UI static export (`npm run build:export` → `frontend/out`, gitignored) được API mount tại `/` trên cùng cổng khi `SERVE_UI=true` (`UI_STATIC_DIR` override, auto-detect `frontend/out`); UI build với `NEXT_PUBLIC_API_BASE_URL=""` (same-origin); static không cần auth, `/v1` vẫn sau auth; bản Docker không đổi (image không có `frontend/out` → tự bỏ qua). Test `tests/test_v31_lite_ui.py`.
  - `install-task.ps1` chạy watchdog với `-WindowStyle Hidden` (không popup console mỗi 5 phút/lần wake); `metasearch-lite.ps1` có mutex theo port (`Local\MetaSearchLiteManager-<port>`) — đã có phiên đang chạy thì bỏ qua, không nhân bản; `start` tự build UI một lần nếu thiếu; thêm action `open`. Script vẫn ASCII-only, parse OK trên PowerShell 5.1.
  - P3 từ docs/24 §8: `POST /v1/spaces/{slug}/graph/build` nay trả 202 chạy nền single-flight (admit tier `kg` mới) + `GET /v1/spaces/{slug}/graph/status` đọc counts từ bảng (survives restart); tokenize kiểm stopword trên token GỐC trước khi fold nên "cổ"/"thị" không bị "có" nuốt, các từ extra va chạm danh từ (sao/do/day/ma/bac/nam) đã loại.
  - OpenAPI snapshot regen (graph/build 202 + `/graph/status` + field mới của GraphBuildResponse).

- ✅ **v3.0.0 Wave P2 (docs/20 §4 → docs/24)** — chi tiết ở [CHANGELOG.md](CHANGELOG.md) và [docs/24-wave-p2-v3.0.md](docs/24-wave-p2-v3.0.md):
  - P2-1 PDF page-level retrieval: `extraction/pdf.py` (pypdf pinned, `## p.N`, bounded, fail-open) hook vào scraper sau check challenge page; vision-embed API tuỳ chọn (`VISION_EMBED_API_BASE_URL`, guard `EMBEDDING_DIM`, chỉ http(s)) ghi cùng cột `chunks.embedding`; persist vẫn embed khi L2 off nhưng vision API có cấu hình.
  - P2-2 KG theo Space (LightRAG-lite): `kg_nodes`/`kg_edges` (alembic 0009, unique constraint → rebuild idempotent), `POST /v1/spaces/{slug}/graph/build|query` (LLM rẻ trích entity, `parse_graph_json` sanitize fold dấu, query entity-centric 1-hop + `answer=true`).
  - P2-3 vi-first: `fold_diacritics()` + `tokenize(fold=True)` mặc định, stopword VI mở rộng, golden eval `evals/golden_v3_vi.json` (25 câu, `scripts/eval_v2.py --golden`).
  - P2-4 online interleaving: variant sha256 deterministic theo (query, owner), `interleave_sig` trong `search_cache_key`, treatment RRF weight, `/v1/search` trả `interleave.variant`, `POST /v1/memory` nhận `payload.variant`, `GET /v1/ops/interleave` (scan bounded 5000 row).
  - P2-5 personalized podcast: `/v1/podcast/personal*` từ watchlist của owner (fallback preset global), file `personal-<sha256[:8]>-<date>.mp3` cùng retention; `PODCAST_PERSONAL_ENABLED`.
  - P2-6 structured JSON output: `SearchRequest.response_format` (jsonschema pinned), response `structured`/`structured_errors` — lỗi thì báo, không bịa. `ResponseFormat` phải định nghĩa TRƯỚC `SearchRequest` (forward-ref của subclass ở module khác).
  - P2-7 domain tiers (`retrieval/tiers.py`): curated `_TRUSTED_DOMAINS` vẫn thắng, ngược lại tier base; `domain_tier` trên mọi result + cache round-trip.
  - 43 test mới (`test_v30_wave_p2.py`); 3 vòng tự audit 8-lens (docs/24 §8 — phát hiện & fix: pdf.py string bị hỏng do heredoc, ResponseFormat ordering làm vỡ OpenAPI, retention prefix_len của file personal, kg unique constraint, interleave scan unbounded, vision dim guard); full pytest xanh, ruff/bandit/mypy sạch, frontend 49 vitest + tsc xanh, OpenAPI snapshot regen (6 endpoint mới).

- ✅ **v2.6.0 Deep Intelligence wave P1 (docs/20 §4 → docs/22)** — chi tiết ở [CHANGELOG.md](CHANGELOG.md) và [docs/22-deep-intelligence-wave-v2.6.md](docs/22-deep-intelligence-wave-v2.6.md):
  - P1-1 IRCoT loop trong `research.py`: reformulate gap theo learnings (`_reformulate_query`) + coverage gate (`_plan_coverage`, `RESEARCH_COVERAGE_THRESHOLD=0.6`), `coverage` trong event `learnings`/`done`.
  - P1-2 adaptive router (`retrieval/router.py`): heuristic classify simple/standard/deep, tự nâng `deep=true` ở `_personalize` (nằm trong cache key).
  - P1-3 late chunking: `segment_long_document` + `late_chunk_markdown` (dùng ở /v1/crawl, Spaces crawl), `EmbeddingClient.embed_many`, persist gán chunk vector của segment (`persist.configure_embedder` ở lifespan), bounded + fail-open.
  - P1-4 hybrid chunk index: cột `chunks.tsv` (TSVECTOR + GIN + backfill trên Postgres, alembic 0008), `retrieval/hybrid.py` BM25 + cosine fuse bằng RRF, endpoint `POST /v1/hybrid` (hỗ trợ `space=`).
  - P1-5 per-key memory (Mem0-lite): bảng `memory_items` (alembic 0008), CRUD `/v1/memory` (+ DELETE xoá toàn bộ — privacy), feedback 👍/👎 gộp thành domain boost (`load_memory_boosts`, vào `boosts_sig`), ghi query qua bounded persist queue (`record_memory=False` cho sub-query research).
  - P1-6 Spaces: bảng `spaces` (alembic 0008), CRUD `/v1/spaces` + `POST /v1/spaces/{slug}/crawl`, `space=<slug>` trên `/v1/search` + `/v1/answer` (domain scope post-cache, pinned URL thành `BoostRule(kind="url")`).
  - P1-7 `/v1/map` + `/v1/crawl` (`extraction/sitemap.py` defusedxml + `api/v1/site.py` BFS same-host qua guarded scraper, persist vào index; `crawl_site` không lồng admission).
  - P1-8 freshness half-life: `freshness(half_life_days=...)`, news giữ đúng legacy (20.794≈30·ln2), non-news 45 ngày; `computed` vẫn luôn 0.
  - 38 test mới (`test_v26_wave_p1.py`); 3 vòng tự audit 8-lens (docs/22 §8); full pytest xanh, ruff/bandit/mypy sạch; OpenAPI snapshot regen (thêm `space` + 6 endpoint mới).

- ✅ **v2.5.0 Retrieval & Answer Quality (docs/20 P0 → docs/21)** — chi tiết ở [CHANGELOG.md](CHANGELOG.md) và [docs/21-retrieval-quality-v2.5.md](docs/21-retrieval-quality-v2.5.md):
  - P0-1 RRF fusion (`RRF_K=60`, weight từ registry) trong `merger.py`, blend với signals, `rrf_weight` config; cache key `sr:v3:`.
  - P0-2 `RERANKER=bge` — `BGEReranker` (bge-reranker-v2-m3, int8 preferred, tự tải HF pin revision về `MODEL_CACHE_DIR`), fail-open về heuristic.
  - P0-3 multi-query expansion (call LLM rẻ, chỉ deep/câu dài, variant chỉ fan-out general engine) + fuse bằng RRF; `expansion_sig` vào cache key.
  - P0-4 claim-level citation check (`check_claims` + lexical entailment) — báo `unsupported_markers`, không sửa text.
  - P0-5 follow-up suggestions (SSE event `follow_ups`, vào answer cache + `AnswerResponse.follow_ups`, UI chip "Hỏi tiếp"), skip khi budget <25%.
  - P0-6 per-key boosts: bảng `key_boosts` (alembic 0007) + CRUD `/v1/prefs/boosts`, áp ở merger sau blend, `boosts_sig` vào cache key, load bounded 0.3s.
  - P0-7 agent extras trên `/v1/search` (`extras=contents,highlights,answer`), tính sau cache, UI nút 📦 Extras.
  - P0-8 eval harness v2: `app/eval_metrics.py` + `evals/golden_v2.json` (29 query EN/VI) + `scripts/eval_v2.py` ghi report + delta so với report trước.
  - 37 test mới (`test_v25_retrieval_quality.py`); 3 vòng tự audit 8-lens (docs/21 §8); full pytest xanh, ruff/bandit/mypy sạch, frontend 49 vitest + tsc + build xanh.

- ✅ **v2.4.0 Global Deep Search (docs/19)** — chi tiết ở [CHANGELOG.md](CHANGELOG.md) và [docs/19-global-deep-search-v2.4.md](docs/19-global-deep-search-v2.4.md):
  - 7 adapter keyless mới: `hackernews`, `stackexchange` (site: đổi site), `bluesky`, `openalex`, `crossref`, `europepmc`, `gdelt` (sourcelang + time window); helper chung `_http.fetch_json` map lỗi đúng Outcome cho breaker.
  - SearXNG bật thêm 15 engine toàn cầu (audit vòng 9 bỏ 4 tên không tồn tại) + `default_lang: auto`; adapter map category MetaSearch→SearXNG (tránh 400 do category lạ).
  - `lang` end-to-end tới google_news (hl/gl/ceid), gdelt (sourcelang), openalex (language filter); UI chọn ngôn ngữ Tự động/VI/EN.
  - `deep: true` trên /v1/search + /v1/answer: fan-out TẤT CẢ engine (bỏ lọc category); cache key đã gồm deep; category inference mở rộng social/academic/global-fin.
  - Upgrade path: DB engine store cũ tự seed engine mới khi thiếu (không đè tweak của operator) — có test. 25 test mới, OpenAPI snapshot regen.

- ✅ **v2.3.0 feature roadmap (docs/18)** — chi tiết ở [CHANGELOG.md](CHANGELOG.md) và [docs/18-feature-roadmap-v2.3.md](docs/18-feature-roadmap-v2.3.md):
  - **A**: dictation vi-VN (Web Speech) ở ô tìm kiếm + ô lọc tin; tab 🔬 Deep Research tiêu thụ FR-9 (SSE live + đọc theo section).
  - **B**: `POST /v1/fa/compare` (≤5 mã, provenance từng field) + tab ⚖️; `POST /v1/market/ohlcv` (VNDIRECT primary/SSI fallback, một parser strict dùng chung, không bao giờ bịa time) + biểu đồ `lightweight-charts@5.3.0` (pinned, API v5).
  - **C**: watchlist theo key (CRUD, cap 20, alembic 0006) + `/v1/watchlist/alerts` poll-on-demand (bỏ background poller — không đốt budget nền); DB chết → memory fallback có giới hạn + re-probe throttle 30s.
  - **D**: podcast bản tin qua VoiceStudio: `/v1/podcast/briefing|status|latest|feed.xml`, idempotent theo ngày, single-flight, retention 7 ngày ở `api/podcast_data/` (gitignored); 503 `tts_disabled` khi chưa cấu hình.
  - **3 vòng audit đối kháng (8 lens: finance, technical/SA, AI, QA, auditor/security, data-engineer, dream-rsi, perf)** — không còn P0/P1, mọi P2 đã fix (chi tiết docs/18 §8); full pytest xanh, ruff/bandit/mypy sạch, frontend 49 vitest + tsc + next build xanh, OpenAPI snapshot khớp.

- 📋 **Backlog nghiên cứu & roadmap đề xuất (docs/20 — 2026-11-12, chưa triển khai)** — xem [docs/20-research-landscape-and-future-roadmap.md](docs/20-research-landscape-and-future-roadmap.md):
  - Đề xuất wave P0 (v2.5): RRF fusion ở `merger.py`, reranker local `bge-reranker-v2-m3` (ONNX), query expansion + RRF, kiểm citation từng claim bằng NLI, follow-up suggestions, custom boosts theo key (Goggles-lite), field agent-friendly cho `/v1/search` (contents/highlights/answer), eval harness v2 (BEIR/MIRACL-vi/RAGAS).
  - Wave P1 (v2.6): vòng lặp IRCoT + coverage gate cho research, adaptive router theo độ phức tạp, late chunking, hybrid sparse (BGE-M3) trong pgvector, memory theo key, Spaces, `/v1/map` + `/v1/crawl`, freshness decay.
  - Wave P2 (v3): ColPali multimodal PDF, KG LightRAG theo space, vi-first BM25 + golden eval VI, online interleaving, voice/podcast cá nhân hoá, structured JSON output, domain reputation tiers.

- ✅ **Multi-source FA, bỏ điểm hụt đơn nguồn (v2.1.0)** — chi tiết ở [CHANGELOG.md](CHANGELOG.md) và [docs/15-fa-sources-plan.md](docs/15-fa-sources-plan.md):
  - Package `app/retrieval/fa_sources/`: adapter **trực tiếp** SSI (OHLCV), TCBS (ratios), FireAnt (BCTC, JWT `FA_FIREANT_TOKEN`) — không qua vnstock/wrapper; 21 nguồn đã thẩm định trong docs/15.
  - `FAAggregator`: fan-out breaker-gated (`fa:{source_id}`), cross-validate 0.5%, conflict ưu tiên priority + log `fa_conflict`, hết nguồn → fields rỗng + `missing_fields` (không ngụy tạo).
  - `VietStockAdapter`: fallback ĐỒNG THỜI khi VNDIRECT/Simplize chết; provenance kết quả ghi đúng nguồn thật (SSI/FA-Aggregator), `name`/`exchange` từ registry DAT-06.
  - 48 test MockTransport (audit 3 expert: SA/QA-perf/finance + probe live, 2 vòng PASS); ruff/bandit/perf sạch. CafeF/VietStock scraper chờ probe HTML schema thật; SSI/TCBS route cần xác minh lại từ egress sản xuất (docs/15 §10).
- ✅ **Web UI tab Tin tức + đọc máy tiếng Việt (v2.2.0)** — chi tiết ở [CHANGELOG.md](CHANGELOG.md) và [docs/16-web-ui-news-tts.md](docs/16-web-ui-news-tts.md):
  - Tab Tin tức 6 mục preset qua `google_news` + map `freshness_days`→`when:{n}d` **server-side** trong adapter (cửa sổ thời gian thật, guard end-anchored không nhân đôi operator; client chỉ gửi param có cấu trúc), không thêm endpoint backend; `safeHref()` gói mọi href từ payload.
  - TTS vi-VN qua Web Speech API (voice stack của OS, free, không egress backend): đọc câu trả lời + đọc bản tin/từng tin; strip markdown/URL/`[n]`, chunk ≤220 ký tự, một speaker tại một thời điểm, generation guard, graceful degradation + ghi chú riêng tư trên tooltip.
  - TTS server-side tuỳ chọn **VoiceStudio** (docs/17): endpoint `/v1/tts/*` sau auth, client env-only URL validate nghiêm, ETag + single-flight + LRU bounded + admission tier riêng; tắt mặc định, Web Speech là default. Audit vòng 8 đối kháng (security/SA + QA/data-integrity): ETag length-prefix, admission trong flight, fallback Web Speech khi VoiceStudio lỗi, 34 test TTS — PASS; tổng 830 backend + 21 frontend tests, 7/7 quality gates.
- ✅ **Readiness cho hệ thống bên ngoài (v2.0.0)** — chi tiết và số đo ở [CHANGELOG.md](CHANGELOG.md):
  - **Breaking:** `/v1/context` mặc định chỉ `chunks` + `sources` (xin thêm qua `include`); BYO LLM tắt mặc định; cách ly dữ liệu theo key; vài route chỉ admin; SuperAgent tắt mặc định.
  - Không còn số liệu tài chính ước lượng hay bảng tĩnh trong runtime; mọi kết quả, chunk có mốc nguồn và thời gian.
  - Ngân sách theo request, admission control, single-flight, lớp HTTP dùng chung, CPU ra khỏi event loop.
  - Metric + alert, ghi DB qua hàng đợi có giới hạn, retention, `/readyz` có nghĩa, Lite chạy Alembic; hoàn tất khắc phục 13 phát hiện kiểm toán F1–F13 (lưu trữ phán quyết council, chu kỳ retention, tối ưu imports).
  - Lite trên Windows như dịch vụ (`scripts/windows/`), kiểm tra dòng lệnh tiến trình qua Win32_Process chống xung đột tái sử dụng PID; hợp đồng tích hợp `docs/15-integration-guide.md`.
  - CI: lockfile `uv`, mypy (`app/core`, `app/api`), coverage ≥ 84%, gitleaks, promtool, nightly; 594 unit & E2E tests pass 100%.
  - Đo trên harness: search c=1 p95 197 ms (trước 875 ms), c=20 0% lỗi (trước 100%), Lite context c=10 0% lỗi (trước 100%). Chưa đo Postgres/Redis/SearXNG thật, Linux, soak dài.
- **Ghi chú cho các mục cũ dưới đây:** "hơn 1,600 mã" thực tế là 1.518 mã; "nghiêm cấm ngụy tạo" ở v1.3.1 chỉ thật từ v2.0.0; SuperAgent (v1.3.0) là thử nghiệm, tắt mặc định.
- ✅ **Financial Data Integrity & Real Market OHLCV Grounding (v1.3.1)**:
  - Loại bỏ triệt để và vĩnh viễn các hàm giả lập nến `sin/cos` (`_build_stock_ohlcv_series`, `_build_asset_fallback_series`).
  - Kết nối trực tiếp dữ liệu nến thật từ **VNDIRECT dchart TradingView Feed** cho 1.518 mã chứng khoán Việt Nam (HSX, HNX, UPCOM; danh mục `app/retrieval/data/vn_tickers.json`, có ngày `as_of`) và chỉ số thị trường (`VNINDEX`, `VN30`).
  - Kết nối dữ liệu cơ bản thực tế từ **Simplize API** với thuật toán động `_build_dynamic_company_financials`.
  - Chuẩn hóa 100% nhãn thời gian `published_at` từ timestamp nến EOD thực tế và ngày cập nhật BCTC, nghiêm cấm ngụy tạo `datetime.now(UTC)`.
  - Ngăn chặn hành vi tự ý inject mã cổ phiếu vào các truy vấn thị trường chung qua bộ lọc `_VIET_STOCK_STOP_WORDS`.
  - Hoàn tất **3-Round Expert Audit** (Solution Architect, Financial Quant / BA, QA/Security/Perf), 374 unit & E2E tests pass 100%, 7 Quality Gates passed.
- ✅ **DeepSeek Super-Agent Architecture (Router-Worker, Observe Mode, Quiet Autonomy & Watchdog Supervisor) (v1.3.0)**:
  - Tích hợp nguyên mẫu tự trị từ **`deepseek-ai/dsh-superagent-prototype`** giải quyết nút thắt chú ý của con người.
  - Phân tách **Router - Worker Decoupling**: Router trung tâm (`SuperAgentRouter`) tiếp nhận và phân rã mục tiêu thành tickets độc lập giao cho các workers cách ly (`IsolatedSuperAgentWorker`).
  - Triết lý **"Quiet Autonomy" & Escalation Queue**: Agent im lặng thực thi ReAct, chỉ thông báo khi có thành phẩm (`SuperAgentArtifact`) hoặc đưa vào `EscalationRequest` khi cần con người phê duyệt.
  - **Observe Mode (Shadow Evaluation)**: Chế độ shadow plan thẩm định không tiêu tốn quota live.
  - **Proactive Event Ingestion**: Tự động kích hoạt tickets từ luồng sự kiện (BCTC mới, CVE, market feed).
  - **Watchdog Supervisor & Circuit Breaker** (thử nghiệm, **tắt mặc định** `SUPERAGENT_ENABLED=false`; chưa sinh báo cáo thật): giám sát heartbeat, ngắt mạch khi worker treo hoặc token burst (`383+ unit & E2E tests pass`).
- ✅ **Multi-Platform Matrix CI (Windows, macOS, Linux) & Pre-Commit Hardening (v1.2.1)**:
  - Cấu hình GitHub Actions CI Matrix đa nền tảng (`ubuntu-latest`, `windows-latest`, `macos-latest`) thực thi song song trên **mọi commit** và PR (4 jobs: backend matrix, frontend matrix, release pipeline gate, docker smoke).
  - Tự động hóa hook `.githooks/pre-commit` nhận diện runtime Python đa nền tảng (`.venv/Scripts/python.exe`, `.venv/bin/python3`, PATH `python3`), tự động cấp quyền `chmod +x` trên Unix, cưỡng chế 7 Quality Gates.
  - Loại bỏ hoàn toàn cờ `shell=True` trong `scripts/release.py`, tuân thủ 100% bảo mật Bandit AST (B602).
- ✅ **Multi-Free-Endpoint LLM Council, Dream-RSI Replay World & Universal Domain Intelligence (v1.2.0)**:
  - Tích hợp gọi song song đồng thời nhiều Free LLM endpoints (`openrouter/free`, `pollinations`, `groq`, `gemini`, `nvidia_nim`, `cerebras`, `sambanova`,...), ẩn danh thành `Candidate A, B, C...` để **Anonymous Cross-Review** lẫn nhau theo thang rubric chuyên ngành mà không bị thiên kiến.
  - Tự động cải tiến chính sách hội đồng qua **Dream-RSI Replay World** (`ReplayTrajectory`, UCB1 exploration score, offline simulation).
  - Tích hợp 2 danh mục Free LLM mở rộng (`nejib1/Free-LLM` và `open-free-llm-api/awesome-freellm-apis` — 20 presets).
  - Kiến trúc **Universal Multi-Domain Deep Intelligence** (`financial` Flagship Pilot + 7 vertical packs: `biomedical_health`, `technology_ai`, `academic_research`, `legal_macro_policy`, `eastern_metaphysics`, `news_geopolitics`, `general_discovery`).
  - Cưỡng chế quy trình release & pre-commit: hook `.githooks/pre-commit` chặn mọi commit nếu trượt bất kỳ kiểm định nào; **366+ unit & E2E tests pass**.
- ✅ **OpenRouter Free & External System LLM Integration (v1.1.0)**: Tích hợp sẵn OpenRouter Free Router (`openrouter/free` — https://openrouter.ai/openrouter/free) và 13 LLM presets (Pollinations keyless free, Google Gemini Free, Groq Free, Cerebras Free, Mistral Free, GitHub Models, HuggingFace Router, Ollama/vLLM local, Custom Enterprise LLM), hot-config API (`/v1/llm/providers`, `/v1/llm/config`, `/v1/llm/test`), cùng Context-Only RAG & Citation Validator (`/v1/context`, `/v1/citations/validate`) cho phép tích hợp 2 chiều với hệ thống ngoài đã có sẵn LLM nội bộ.
- ✅ **Institutional Release (v1.0.0)**: Hoàn tất rà soát 5-Wave Deep Audit (SA, BA/PO, QA/Security/Perf), nâng cấp pháp y BCTC chuyên sâu theo đặc thù ngành (CAMELS Ngân hàng, Margin/FVTPL Chứng khoán, Người mua trả tiền trước BĐS/KCN, Altman Z, Beneish M, Piotroski F), chỉ báo kỹ thuật định lượng (True EMA MACD, Wilder's RSI/ATR, Stochastic, EMA Ribbon, Fibonacci Golden Pocket & Extensions, Ichimoku Chikou, Linear Regression Channel $\pm 2\sigma$, 8 Chart Patterns, Custom Indicator Query Parser), 19 Search Engines, 40 Golden Evals (`--judge-faithfulness`), **352 unit tests pass**, 0 ruff/bandit issues, Next.js build sạch.
- ✅ **Lite profile (v0.6.0)**: chạy KHÔNG Docker — DuckDuckGo keyless thay
  SearXNG, SQLite thay Postgres (dialect variants), Jina keyless thay crawl4ai,
  cache + rate limit in-process fallback khi không Redis. API ~100MB RAM.
  Quick start: `deploy/.env.lite.example`; docs/08 §Lite. Docker vẫn là full
  profile (SearXNG consensus, crawl4ai, pgvector) — cùng một bộ code.
- ✅ **Phase 0-4 + Phase 5 hoàn tất** (v1.0.0 — xem [CHANGELOG.md](CHANGELOG.md)):
  toàn bộ FR-1..FR-16 hoàn chỉnh; Deep Research (planner/executor/synthesizer
  + async job + SSE + durable DB recovery), MCP server 4 tools (stdio + HTTP), auth API key + RBAC admin + sliding-window
  rate limit, 14 vertical engines (19 engines tổng cộng),
  eval harness (40 queries, hit@5/citation accuracy/faithfulness judge), Langfuse + OTel optional.
- ⏭️ **Còn lại (cần Docker)**: chạy k6 trên compose, integration tests thật,
  backup job + runbook vận hành.
- ⏭️ Ý tưởng sau v0.5: RAGAS LLM-judge vào eval harness, Generative UI cho
  research report, multi-tenant auth (users join).

### Ma trận tính năng (FR từ docs/01)

| FR | Tính năng | Trạng thái |
|---|---|---|
| FR-1 | Search SearXNG 1 request | ✅ |
| FR-2 | Tavily/Serper/Brave adapters | ✅ (cả 3) |
| FR-3 | Vertical engines & Financial Forensics / Technicals (arXiv, SO, Reddit, Wiki, GitHub, PubMed, HF, S2, Yahoo Fin, Stooq, SEC, Google News, FengShui, VietStock BCTC/Forensics/Fibo/Ichimoku) | ✅ (14 vertical engines - 19 total: viet_stock HSX/HNX BCTC sâu, Stooq global macro, Yahoo Fin, SEC, Google News, FengShui,...) |
| FR-4 | Dedup canonical + SimHash + consensus | ✅ |
| FR-5 | Diversity cap | ✅ |
| FR-6 | Extract chain (trafilatura→crawl4ai→jina) | ✅ |
| FR-7 | Rerank heuristic/ONNX/API | ✅ |
| FR-8 | Answer SSE + citations | ✅ |
| FR-9 | Deep Research async | ✅ |
| FR-10 | MCP 4 tools | ✅ |
| FR-11 | Engine hot config (PATCH, không restart) | ✅ |
| FR-12 | Health + auto-disable + recovery | ✅ |
| FR-13 | Cache L1 Redis + L2 pgvector | ✅ (L2 default off) |
| FR-14 | Auth API key + rate limit | ✅ (AUTH_ENABLED=true để bật) |
| FR-15 | Web UI (search/answer/sources) | ✅ |
| FR-16 | Tiếng Việt (lang, VN stopwords, prompt same-language) | ✅ cơ bản |

### Cần cải thiện (chưa tốt — đánh khi có dịp)

1. **L2 semantic cache mặc định off** — plumbing xong; cần guide + test với
   TEI/Ollama thật.
2. **SO/Reddit adapters** — ✅ xong ở v0.7.0 (cả 4/4 vertical engines hoàn tất).
3. **Chunk scoring dùng word-proxy** — tiktoken có sẵn nhưng mặc định words;
   đo lại chất lượng context khi bật.
4. **Eval harness mở rộng** — ✅ golden set mở rộng 30 queries đa dạng (v0.7.0).
   (thêm RAGAS LLM-judge tiếp theo).
5. **Deep research jobs durable** — ✅ đã hỗ trợ tự động phục hồi finished report từ DB khi restart server (v0.7.0).
   (bảng researches riêng nếu cần phân tán nhiều worker).
6. **MCP integration test với SDK thật** — `pip install '.[mcp]'` + smoke với
   Claude Desktop / MCP inspector.

## 3. Bản đồ repo

```
api/app/
  api/v1/        search.py answer.py research.py scrape.py engines.py queries.py fa.py market.py watchlist.py podcast.py tts.py digests.py llm.py ops.py
  api/deps.py    DI providers (lru_cache + reset_pipelines) — endpoint resolve qua deps.get_X()
  core/          errors.py (error model chuẩn) · logging · middleware · metrics
  retrieval/     adapters/ (searxng, tavily) · registry.py · engine_store.py ·
                 merger.py · scoring.py · simhash.py · reranker.py · pinning... (extraction)
  extraction/    ssrf.py · pinning.py · robots.py · scraper.py (fallback chain) · chunker.py
  llm/           gateway.py (OpenAI-compat + Anthropic streaming) · prompts.py ·
                 citations.py · embeddings.py · tokens.py
  orchestration/ pipeline.py (search + answer, event generator) · persist.py · engine_store.py
frontend/        app/page.tsx (UI) · lib/api.ts (SSE client + ApiError)
deploy/          docker-compose.yml · searxng/ · prometheus/ · grafana/ · k6/
docs/            01-29 thiết kế + adr/ (25 AI infra, 26 architect review, 27 sentiment standards, 28 rich UI, 29 unified digest) — 12-roadmap-plan.md là source of truth; README.md song ngữ Anh–Vi trong một file (mục #english / #tiếng-việt)
```

## 4. Quy ước bắt buộc

- **Giants-first**: trước khi tự viết, hỏi "giant nào làm sẵn?" (ADR-007).
  Không thêm SDK nếu httpx làm được (LLM gateway là ví dụ).
- **Graceful degradation**: engine lỗi → trả một phần; DB chết → in-memory
  fallback; Redis chết → cache miss; reranker lỗi → giữ heuristic order.
  KHÔNG BAO GIỜ để 1 dependency chết làm fail request.
- **Error model**: mọi lỗi đi qua `core/errors.py` (`MetaSearchError` +
  `ErrorCode`); client dùng `ApiError`. Không trả lỗi dạng khác.
- **Endpoints resolve dependencies qua `deps.get_X()`** (không import trực tiếp
  function vào namespace) để tests monkeypatch được.
- **Persist/cache writes**: fire-and-forget hoặc bounded — không nằm chặn
  response (bài học perf: DB partition treo 30s).
- **SSRF**: mọi fetch URL-user-supplied phải qua `ssrf.resolve_and_check` +
  `pinning.build_pinned_request`; redirect re-check từng hop; cap 5MB.
- **Code style**: `ruff check` + `ruff format` (line 100, py312) phải sạch;
  `bandit -q -r app -x tests` phải 0 issue; comment tiếng Anh, tài liệu tiếng Việt.
- **Không ngụy tạo dữ liệu**: không ước lượng, không bảng tĩnh, không giá trị mặc định
  giả làm số thật. Thiếu nguồn thì trả `null` hoặc rỗng, kèm mốc `source_url`, `as_of`.
- **Không dựng client HTTP tại chỗ**: dùng pool trong `app/http.py` (`search`, `scrape`,
  `llm`, `internal`); dựng client mới tốn hàng trăm ms và làm hỏng giới hạn kết nối.
- **Không chạy việc CPU nặng trên event loop**: parse HTML, chunk, score, rerank đi qua
  `app.core.offload.run_cpu`; có test spy để đặt lại lên loop là đỏ.
- **Dữ liệu chạy cục bộ (Local runtime data) — KHÔNG commit, KHÔNG xoá khi chủ chưa xác nhận**:
  các DB SQLite trong `api/` (`*.db`, `*.db-wal`, `*.db-shm`, `*.sqlite3`), tệp mốc `*.retention` và
  `api/.env.*` (trừ `*.example`) là dữ liệu THẬT của các hệ thống tích hợp (khoá API, user, lịch sử),
  không phải tệp tạm, dù đã gitignore. Xoá một DB là khoá mọi client tích hợp ra ngoài (401) tới khi cấp
  và triển khai lại khoá — đã xảy ra 10/11/2026 khi dọn dẹp. Quy tắc cho mọi agent và người:
  không xoá, di chuyển, `git clean -x/-X`, hay `keys revoke` các tệp/khoá này khi chủ chưa xác nhận
  trong cuộc trò chuyện; dọn dẹp thì liệt kê và hỏi trước. Không commit chúng lên repo public (kể cả
  `git add -f`). Chốt: `.gitignore`, cổng `.githooks/pre-commit` từ chối khi stage các tệp này,
  hook Claude Code `scripts/guard_local_data.py` (`.claude/settings.json`) buộc hỏi chủ trước lệnh xoá.
- **Tests**: pytest-asyncio auto mode; fakes inject qua `app.api.deps`
  (xem `tests/test_api_v1.py` fixture `stack`); DNS được fake trong conftest;
  integration test gated `METASEARCH_INTEGRATION=1`.

## 5. Lệnh thường dùng

```bash
cd api
.venv/Scripts/python.exe -m pytest            # unit suite (Windows/Git Bash)
.venv/Scripts/python.exe -m ruff check app tests scripts --fix
.venv/Scripts/python.exe -m ruff format app tests scripts
.venv/Scripts/python.exe -m bandit -q -r app -x tests
.venv/Scripts/python.exe scripts/perf_load.py # perf check (không cần Docker)
.venv/Scripts/python.exe -m uvicorn app.main:app --reload
cd ../frontend && npm run typecheck && npm run build

# Lệnh tự động hóa kiểm định và release:
python scripts/release.py --clean-only        # dọn dẹp sạch tmp, caches, stale DBs
python scripts/release.py --check             # chạy toàn bộ quality gates
python scripts/release.py --bump patch --push # bump version, tag, update changelog & push

# Stack đầy đủ (máy có Docker):
cd ../deploy && docker compose up -d && docker compose --profile observability up -d
METASEARCH_INTEGRATION=1 pytest tests/integration -q   # từ api/
```

## 6. Nhận việc — checklist

0. **Nhận việc dang dở của phiên/agent khác (vd: đọc từ log zcode):** đừng chỉ làm theo
   todo/plan còn lại — trước tiên đọc **toàn bộ diff** phần họ đã làm, tự verify với
   backend contract (schema, cache key, response shape), rồi audit đối kháng tìm lỗi MỚI
   (có thể spawn expert read-only). Trước khi push phải mirror đủ các gate của CI — kể cả
   `uv.lock` khớp version (`check_lockfile_version` trong pre-commit gate; CI chạy
   `uv sync --locked` sẽ đỏ nếu lockfile stale dù gate local xanh).
1. Đọc `docs/12-roadmap-plan.md` (plan + KPI + rủi ro) và mục 2 ở trên.
2. Việc mới thuộc phase nào? Nếu phase trước còn nợ (mục "Cần cải thiện"),
   cân nhắc xử lý nợ trước khi mở phase mới.
3. Implement theo quy ước mục 4; thêm tests cho từng module mới.
4. Chạy đủ: pytest + ruff + bandit (+ frontend typecheck/build nếu đụng UI).
5. Tự review tối thiểu 2 lần (pattern đã dùng từ Phase 1) — lần 1 từng module,
   lần 2 fresh-eyes + smoke test live.
6. **3 Quy tắc Cứng về Ranh giới (Hard Boundary Rules — bắt buộc khi thêm/sửa route `/v1/*`, kiểm tự động bởi `tests/test_w5_guardrails.py`)**:
   - **Rule 1 (Rate-Limit Weight & Admission Tier)**: Mọi route `POST` mới bắt buộc phải được phân loại trọng số trong `_STATIC_ROUTE_COSTS` (`app/core/auth.py`) nếu gọi LLM/TTS/crawl/fan-out, hoặc khai báo tường minh vào `UNIT_COST_POST` (`tests/test_w5_guardrails.py`). Mọi lệnh gọi `async with admit("<tier>")` phải có `<tier>` khai báo trong `_TIER_CONFIG` (`app/core/admission.py`).
   - **Rule 2 (Strict Input Upper Bounds)**: Mọi trường `str`, `list`, và tham số số nguyên (`limit`, `offset`, `days`, `max_pages`, `max_urls`, `depth`, `breadth`, `k`) trong Pydantic request schema, `Query(...)` và `Path(...)` bắt buộc phải có cận trên (`max_length`, `StringConstraints(max_length=...)`, `le`).
   - **Rule 3 (Tenancy & Operator Surface Authorization)**: Mọi truy vấn ORM trên bảng có dữ liệu người dùng (`Query`, `WatchlistItem`, `KeyBoost`, `MemoryItem`, `Space`, `KgNode`, `KgEdge`) phải lọc theo `api_key_id` của caller (trả `404` khi lệch chủ sở hữu). Các route vận hành/chẩn đoán hoặc ghi tài nguyên dùng chung phải gắn `Depends(require_admin)` hoặc `Depends(require_operator)`.
7. Cập nhật `docs/12-roadmap-plan.md` (checkbox + verify table),
   `CHANGELOG.md` (version mới nếu đáng nhớ), và mục "Trạng thái" ở đây.
