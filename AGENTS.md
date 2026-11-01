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

## 2. Trạng thái hiện tại (cập nhật: 2026-11-02 — v1.2.0 Multi-Free-Endpoint Council, Dream-RSI & Universal Intelligence)

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
  api/v1/        search.py answer.py scrape.py engines.py queries.py ops-status
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
docs/            01-12 thiết kế + adr/ (8 ADR) — 12-roadmap-plan.md là source of truth
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

1. Đọc `docs/12-roadmap-plan.md` (plan + KPI + rủi ro) và mục 2 ở trên.
2. Việc mới thuộc phase nào? Nếu phase trước còn nợ (mục "Cần cải thiện"),
   cân nhắc xử lý nợ trước khi mở phase mới.
3. Implement theo quy ước mục 4; thêm tests cho từng module mới.
4. Chạy đủ: pytest + ruff + bandit (+ frontend typecheck/build nếu đụng UI).
5. Tự review tối thiểu 2 lần (pattern đã dùng từ Phase 1) — lần 1 từng module,
   lần 2 fresh-eyes + smoke test live.
6. Cập nhật `docs/12-roadmap-plan.md` (checkbox + verify table),
   `CHANGELOG.md` (version mới nếu đáng nhớ), và mục "Trạng thái" ở đây.
