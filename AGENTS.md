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

## 2. Trạng thái hiện tại (cập nhật: 2026-11-12 — v2.4.0 Global Deep Search docs/19: 7 engine toàn cầu mới, EN end-to-end, deep mode)

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
docs/            01-19 thiết kế + adr/ (16/17 WebUI+TTS/VoiceStudio, 18 roadmap v2.3, 19 global search) — 12-roadmap-plan.md là source of truth; README.md song ngữ Anh–Vi trong một file (mục #english / #tiếng-việt)
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
6. Cập nhật `docs/12-roadmap-plan.md` (checkbox + verify table),
   `CHANGELOG.md` (version mới nếu đáng nhớ), và mục "Trạng thái" ở đây.
