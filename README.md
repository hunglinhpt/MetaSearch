# MetaSearch — AI Meta-Search, Institutional Financial Intelligence & Deep Research Engine

> Hệ thống tìm kiếm đa nguồn & phân tích định lượng phục vụ AI: **Meta-Search (SearXNG 70+ engines + 14 Vertical Engines) → Crawl & Extract → Dedup/Consensus/Rerank → Synthesize có trích dẫn `[n]`**, đóng gói thành REST API + SSE Streaming + MCP Server + Automated Daily Briefings Blog + Web UI.

Trạng thái: **v2.0.0 — Readiness cho hệ thống bên ngoài (an toàn, liêm chính dữ liệu, tải, quan sát, vận hành, CI)**:
- **Sẵn sàng cho hệ thống bên ngoài (`v2.0.0`)**:
  - `/v1/context` mặc định chỉ trả `chunks` + `sources` (xin thêm qua `include`); BYO LLM tắt mặc định; cách ly dữ liệu theo API key; vài route chỉ admin; SuperAgent tắt mặc định.
  - Không số liệu tài chính ước lượng hay bảng tĩnh trong runtime; mọi kết quả, chunk có mốc nguồn (`source_url`, `as_of`) và thời gian.
  - Ngân sách theo request, admission control, single-flight, lớp HTTP dùng chung, CPU ra khỏi event loop.
  - Metric + alert, ghi DB qua hàng đợi có giới hạn, retention, `/readyz` có nghĩa, Lite chạy Alembic.
  - Lite trên Windows như dịch vụ (`scripts/windows/`), tự động kiểm tra dòng lệnh tiến trình (`uvicorn ... app.main:app`) qua Win32_Process để triệt tiêu va chạm tái sử dụng PID sau sự cố/reboot; hợp đồng tích hợp `docs/15-integration-guide.md`.
  - Hoàn tất khắc phục triệt để 13 phát hiện kiểm toán chuyên sâu F1–F13 (lưu trữ bền vững phán quyết LLM Council, chu kỳ retention, tối ưu imports module).
  - CI: lockfile `uv`, mypy (`app/core`, `app/api`), coverage ≥ 84%, gitleaks, promtool, nightly.
  - Đo trên harness: search c=1 p95 197 ms (trước 875 ms), c=20 0% lỗi (trước 100%), Lite context c=10 0% lỗi (trước 100%). Chưa đo Postgres/Redis/SearXNG thật, Linux, soak dài.
- **Liêm Chính Dữ Liệu Tài Chính & Nguồn Công Khai Thực Tế (`v1.3.1`)**:
  - Loại bỏ triệt để 100% các chuỗi nến giả lập bằng hàm `sin/cos`, kiên quyết thực hiện nguyên tắc **Zero-Hallucination & Grounded Data**.
  - Kết nối trực tiếp dữ liệu nến thật từ **VNDIRECT dchart TradingView Feed** cho 1.518 mã chứng khoán Việt Nam (HSX, HNX, UPCOM; danh mục `app/retrieval/data/vn_tickers.json`, có ngày `as_of`) và chỉ số thị trường (`VNINDEX`, `VN30`).
  - Kết nối dữ liệu cơ bản thực tế từ **Simplize API** với thuật toán động `_build_dynamic_company_financials`.
  - Chuẩn hóa liêm chính mốc thời gian `published_at` theo ngày nến EOD thực tế và ngày cập nhật BCTC, nghiêm cấm ngụy tạo `datetime.now(UTC)`.
  - Bộ lọc `_VIET_STOCK_STOP_WORDS` ngăn chặn việc tự ý inject mã cổ phiếu vào các truy vấn thị trường chung.
  - Hoàn tất **3-Round Expert Audit** (Solution Architect, Financial Quant / BA, QA/Security/Perf), 698 unit & E2E tests pass 100%, 7 Quality Gates passed.
- **Kiến Trúc Autonomous Super-Agent Engine (`v1.3.0`)**: Tích hợp các tinh hoa kiến trúc từ nguyên mẫu **`deepseek-ai/dsh-superagent-prototype`** kết hợp với nền tảng MetaSearch:
  - **Router - Worker Decoupling (`app.orchestration.superagent.router`)**: Tách biệt Bộ điều phối trung tâm (`SuperAgentRouter`) quản lý hàng đợi tickets khỏi các công nhân tính toán độc lập (`IsolatedSuperAgentWorker`), giải quyết nút thắt chú ý của con người.
  - **Triết Lý "Quiet Autonomy" & Human Escalation**: Agent tự chủ im lặng thực thi đa bước ReAct và chỉ thông báo khi có thành phẩm hoàn chỉnh (`SuperAgentArtifact`) hoặc phát sinh tình huống rủi ro cao cần con người ra quyết định (`EscalationRequest`).
  - **Observe Mode (Shadow Evaluation)**: Chế độ chạy ngầm thẩm định và lập kế hoạch hành động mà không tiêu tốn tài nguyên live API; tự động kích hoạt khi có webhook/event.
  - **Proactive Event Ingestion (`app.orchestration.superagent.events`)**: Lắng nghe các luồng sự kiện thời gian thực (báo cáo BCTC mới, tin tức thị trường, cảnh báo CVE bảo mật, GitHub issues) để tự động kích hoạt ticket chuyên ngành.
  - **Watchdog Supervisor & Circuit Breaker (`app.orchestration.superagent.watchdog`)**: Giám sát nhịp tim worker, tự động ngắt mạch (trip circuit) và phục hồi khi phát hiện task bị treo, vượt ngưỡng latency hoặc bùng nổ token (`TOKEN_BURST`).
- **Multi-Platform Matrix CI trên mọi commit (`v1.2.1`)**: GitHub Actions chạy song song trên cả 3 hệ điều hành **Windows (`windows-latest`), macOS (`macos-latest`), và Linux (`ubuntu-latest`)** với 4 jobs tự động hóa (Backend tests, Frontend build, Release pipeline gate, Docker smoke test) cho mỗi commit và Pull Request.
- **Pre-commit Hook Đa Nền Tảng Tự Động**: `.githooks/pre-commit` tự động nhận diện Python (`.venv/Scripts/python.exe`, `.venv/bin/python3`, PATH `python3/python`), tự động cấp quyền `chmod +x` trên Unix, cưỡng chế 7 Quality Gates trước khi cho phép commit.
- **19 Search Engines** (5 General/Keyed: `searxng`, `ddg`, `tavily`, `serper`, `brave` + **14 Vertical Engines**: `viet_stock`, `stooq`, `yahoo_finance`, `sec_edgar`, `fengshui`, `google_news`, `arxiv`, `pubmed`, `semanticscholar`, `github`, `huggingface`, `stackoverflow`, `reddit`, `wikipedia`) kèm cơ chế **Smart Category Routing** tự động kích hoạt chuyên ngành.
- **Hội Đồng Phán Quyết Đa Endpoint (LLM Council) & Anonymous Cross-Review**: Gọi đồng thời nhiều Free LLM endpoints song song (`openrouter/free`, `pollinations`, `groq`, `gemini`, `nvidia_nim`, `cerebras`, `sambanova`), ẩn danh thành `Candidate A, B, C...` để các model chấm chéo theo thang rubric chuyên ngành mà không bị bias, kết hợp điểm kiểm toán định lượng tất định (`deterministic_score` 40%) và điểm bình duyệt (`peer_score` 60%) để Chủ tịch (`Chairman`) tổng hợp phán quyết tối hậu.
- **Dream-RSI (Recursive Self-Improvement via Replay World)**: Lưu trữ quỹ đạo phán quyết vào **Replay Buffer**, tính điểm tiềm năng UCB1, mô phỏng lại các truy vấn lịch sử qua thế giới ảo (Dream Replay) để tự động tiến hóa chính sách chọn lựa hội đồng và tinh chỉnh nhiệt độ tối ưu theo từng miền chuyên sâu.
- **Tích Hợp Danh Mục Free LLM Mở Rộng (20 Presets)** từ [nejib1/Free-LLM](https://github.com/nejib1/Free-LLM) & [awesome-freellm-apis](https://github.com/open-free-llm-api/awesome-freellm-apis) kèm OpenRouter Free Router ($0/M) và BYO-LLM.
- **Kiến Trúc Universal Multi-Domain Deep Intelligence**: Khả năng thẩm định bản chất chuyên sâu đa lĩnh vực với **Tài chính & Pháp y BCTC (`financial`)** giữ vai trò **Flagship Pilot** cùng 7 gói chuyên sâu (`biomedical_health`, `technology_ai`, `academic_research`, `legal_macro_policy`, `eastern_metaphysics`, `news_geopolitics`, `general_discovery`).
- **Thẩm Định Pháp Y BCTC Chuyên Sâu (`financial_forensics.py`)**: Phân nhánh theo đặc thù ngành — **Ngân hàng (CAMELS / Basel II-III: CAR, NPL, Nợ nhóm 2, LLR, NIM, CASA, LDR, CIR)**, **Chứng khoán (Margin/VCSH so với trần 200%, FVTPL/HTM/AFS, Tỷ lệ an toàn tài chính)**, **Bất động sản & KCN (Người mua trả tiền trước / Của để dành, Tồn kho/Tài sản)**, **Sản xuất (Cấu trúc nợ, DSO, CFO/Net Income)** kèm **Altman Z-Score**, **Beneish M-Score**, **Piotroski F-Score (9 tiêu chí)**.
- **Phân Tích Kỹ Thuật Định Lượng (`technical_analysis.py`)**: **Fibonacci Retracement & Golden Pocket (`61.8%–65.0%`) + Extensions (`127.2%`, `261.8%`)**, **Ichimoku Kinko Hyo (Tenkan/Kijun/Senkou A-B/Chikou Span)**, **True EMA-based MACD**, **Wilder's RSI & 2x ATR Trailing Stop**, **Stochastic (`%K`, `%D`)**, **Multi-EMA Ribbon (`20/50/200`)**, **Kênh hồi quy tuyến tính $\pm 2\sigma$ ($R^2$)**, **8 mẫu hình giá/nến**, và **Custom Indicator Query Parser** (`RSI 9`, `MACD 8 21 5`, `Bollinger 15 2.5`...).
- **Quy Trình Pre-Commit & Release Nghiêm Ngặt**: Cưỡng chế bắt buộc hook `.githooks/pre-commit` chặn mọi commit nếu trượt bất kỳ kiểm định nào; **594 unit & E2E tests PASS**, `ruff` 0 lỗi, `bandit` 0 lỗi bảo mật, Next.js 15 build sạch. Thiết kế đầy đủ nằm trong [docs/](docs/), plan chi tiết ở [docs/12-roadmap-plan.md](docs/12-roadmap-plan.md).

---

## 1. MetaSearch giải quyết vấn đề gì?

| Vấn đề thực tế | Cách MetaSearch xử lý |
|---|---|
| LLM bị "ảo giác", kiến thức cũ | Mọi câu trả lời được tổng hợp từ trang web & báo cáo thật, **bắt buộc có trích dẫn `[n]`** map ngược về nguồn; citation được validate sau khi sinh |
| Một search engine = một góc nhìn | Fan-out đồng thời **70+ engines** qua SearXNG + **14 Vertical Adapters** chuyên sâu (Tài chính chứng khoán VN/Quốc tế, Pháp lý SEC, Y sinh PubMed, Học thuật arXiv/S2, Code GitHub/SO/HF, Tin tức Google News, Phong thủy Bát Trạch) |
| Phân tích cổ phiếu chỉ đọc chỉ số thô (P/E, ROE) | Bóc tách **Pháp y BCTC theo ngành** (CAMELS ngân hàng, Margin/FVTPL chứng khoán, Unbilled Bookings BĐS/KCN, Altman Z, Beneish M, Piotroski F) kết hợp **Kỹ thuật định lượng** (Fibonacci Golden Pocket, Ichimoku Cloud, True EMA MACD, Wilder's RSI/ATR, Kênh hồi quy $\pm 2\sigma$, 8 mẫu hình giá/nến) |
| Nội dung web đầy rác HTML/JS/ads/CAPTCHA | Extraction pipeline (`trafilatura` → `crawl4ai` → `Jina Reader`) kèm SSRF IP-pinning & Anti-challenge escalation biến URL → **Markdown sạch tối ưu cho context LLM** |
| Kết quả trùng lặp, kém liên quan | Canonical URL + SimHash 64-bit dedup → consensus scoring + curated domain authority → **rerank heuristic-first (0ms)**, tuỳ chọn int8 ONNX hoặc API rerank |
| AI Agent cần công cụ tìm kiếm | **MCP Server** với 4 tool: `web_search`, `web_scrape`, `web_search_and_scrape`, `deep_research` (stdio + HTTP) |
| Chi phí API search hàng tháng | Self-host SearXNG hoặc chạy **Lite Profile** (DuckDuckGo keyless + SQLite + Jina keyless) = **$0 cho phần search**, API chỉ ~100MB RAM |

## 2. Kiến trúc một trang

```mermaid
flowchart LR
    subgraph Clients
        UI[Web UI Next.js<br/>Search · Answer · Blog]
        AGENT[AI Agent / MCP Client]
        DEV[Developer / REST]
    end

    subgraph Interface["Interface Layer"]
        API[FastAPI Gateway<br/>REST + SSE + RBAC]
        MCP[MCP Server<br/>FastMCP 4 Tools]
    end

    subgraph Orchestration["Orchestration Layer"]
        QU[Smart Category Routing<br/>intent · lang · profile]
        PL[Planner / Harness²]
        EX[Executor<br/>fan-out · budget]
        SYN[Synthesizer<br/>citations]
        SCN[Automated Scanner<br/>Daily Briefings Blog]
    end

    subgraph Retrieval["Retrieval & Quantitative Layer"]
        SA[19 Search Adapters<br/>SearXNG · DDG · Tavily · Serper · Brave<br/>VietStock · Stooq · YahooFin · SEC<br/>arXiv · PubMed · S2 · GitHub · HF · SO · Reddit · Wiki · News · FengShui]
        QUANT[Financial Forensics & Technicals<br/>CAMELS · Altman Z · Beneish M · Piotroski F<br/>Fibonacci · Ichimoku · MACD · Wilder RSI/ATR]
        MG[Merger<br/>dedup · SimHash · consensus · diversity]
        SC[Scraper Pool<br/>trafilatura · crawl4ai · Jina]
        RR[Reranker<br/>heuristic-first · ONNX · API]
    end

    subgraph Platform["Platform Layer"]
        LLM[LLM Gateway<br/>OpenAI · Anthropic · Ollama · vLLM]
        DB[(Postgres + pgvector / SQLite)]
        RD[(Redis / In-Process Cache)]
        OBS[Observability<br/>OTel · Prometheus · Langfuse]
    end

    UI --> API
    DEV --> API
    AGENT --> MCP
    MCP --> API
    API --> QU --> PL --> EX
    SCN --> EX
    EX --> SA --> QUANT --> MG --> SC --> RR --> SYN
    SYN --> LLM
    EX -.-> RD
    SYN --> DB
    API -.-> OBS
```

Chi tiết đầy đủ: [docs/02-system-architecture.md](docs/02-system-architecture.md) · Pipeline từng stage: [docs/03-search-pipeline.md](docs/03-search-pipeline.md)

## 3. Quick Start

### Lite profile — KHÔNG cần Docker (máy yếu, API ~100MB RAM, v0.6.0+)

```bash
git clone https://github.com/hunglinhpt/MetaSearch.git
cd MetaSearch/api
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"     # Windows; Linux: .venv/bin/pip
cp ../deploy/.env.lite.example .env   # DuckDuckGo keyless + SQLite + Jina keyless + 14 Vertical Engines
.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
# Swagger: http://localhost:8000/docs
```

### Full profile — Docker compose (SearXNG consensus + crawl4ai + pgvector)

```bash
git clone https://github.com/hunglinhpt/MetaSearch.git
cd MetaSearch
cp deploy/.env.example .env          # điền LLM_API_KEY
docker compose -f deploy/docker-compose.yml up -d
# SearXNG   : http://localhost:8080
# API       : http://localhost:8000/docs
# Web UI    : http://localhost:3000

curl -X POST http://localhost:8000/v1/search \
  -H "Content-Type: application/json" \
  -d '{"query": "Phân tích BCTC pháp y và kỹ thuật Fibonacci Ichimoku cổ phiếu FPT RSI 9", "lang": "vi"}'

# CLI
metasearch search "Phân tích cổ phiếu FPT và VCB"
metasearch answer "Phân tích cổ phiếu FPT và VCB" --stream
```

Web UI: mở `http://localhost:3000` — chọn chuyên mục (Tài chính & Cổ phiếu, Tin tức, Lập trình, Học thuật & Y sinh, Phong thủy...) và chế độ **✨ Trả lời AI**, **🔎 Tìm kiếm**, hoặc chuyển sang tab **📰 Bản Tin & Blog Chuyên Sâu**.

## 4. Cấu trúc repo

```
MetaSearch/
├── README.md                  ← bạn đang ở đây
├── AGENTS.md                  ← hướng dẫn AI agent nhận việc (trạng thái, quy ước, checklist)
├── CHANGELOG.md               ← lịch sử version (Keep a Changelog — v1.0.0)
├── CONTRIBUTING.md            ← guideline setup + workflow + gate
├── docs/                      ← thiết kế chi tiết (12 tài liệu + 8 ADR)
│   ├── 01-product-requirements.md
│   ├── 02-system-architecture.md
│   ├── 03-search-pipeline.md
│   ├── 04-agents-orchestration.md
│   ├── 05-data-model.md
│   ├── 06-api-spec.md
│   ├── 07-mcp-integration.md
│   ├── 08-deployment.md
│   ├── 09-observability-eval.md
│   ├── 10-security-compliance.md
│   ├── 11-testing-strategy.md
│   ├── 12-roadmap-plan.md     ← plan chi tiết Phase 0–5 (gồm KPI + verify matrix)
│   └── adr/                   ← 8 Architecture Decision Records
├── api/                       ← FastAPI orchestration + Quantitative Engines
│   ├── app/
│   │   ├── core/              ← auth (API key + RBAC + sliding-window), errors, logging, middleware, metrics
│   │   ├── db/                ← SQLAlchemy dialect-aware models (Postgres+pgvector & SQLite)
│   │   ├── api/               ← ops (/healthz, /readyz, /metrics) + v1 (search, answer, scrape, research, digests, engines, queries, ops_status)
│   │   ├── retrieval/         ← 19 adapters, financial_forensics.py, technical_analysis.py, registry, merger, scoring, simhash, reranker
│   │   ├── extraction/        ← ssrf, pinning, robots, scraper (trafilatura→crawl4ai→jina), chunker
│   │   ├── orchestration/     ← pipeline (search+answer), research (Harness² 8 profiles), research_jobs, scheduler (Daily Briefings), persist, engine_store
│   │   ├── mcp/               ← FastMCP server + REST client (4 tools, stdio + HTTP)
│   │   └── llm/               ← gateway streaming, prompts, citations, embeddings, tokens, langfuse
│   ├── alembic/versions/      ← migrations (Postgres + pgvector / SQLite auto-bootstrap)
│   ├── evals/golden.json      ← 40 golden evaluation queries đa miền (EN + VI)
│   ├── scripts/               ← eval_run.py (--judge-faithfulness) + perf_load.py
│   ├── tests/                 ← 333 unit tests (+ 7 integration tests gated)
│   └── pyproject.toml
├── frontend/                  ← Next.js 15 + React 19 (Web UI streaming + Category Chips + Markdown Table + Blog Reader)
│   ├── app/ · lib/api.ts      ← SSE client + ApiError + Digests API
│   └── package.json
├── deploy/
│   ├── docker-compose.yml     ← stack đầy đủ + profile observability
│   ├── searxng/ · prometheus/ · grafana/ · k6/
│   └── .env.example · .env.lite.example
└── .github/workflows/ci.yml   ← CI: lint + unit test + security scan + build
```

## 5. Bản đồ tài liệu

| Tài liệu | Trả lời câu hỏi |
|---|---|
| [01 Product Requirements](docs/01-product-requirements.md) | Ai dùng, dùng để làm gì, tiêu chí chấp nhận? |
| [02 System Architecture](docs/02-system-architecture.md) | Các tầng, component, luồng dữ liệu, công nghệ, hardware profile 16GB? |
| [03 Search Pipeline](docs/03-search-pipeline.md) | 1 câu hỏi đi qua 11 stage như thế nào, latency budget? |
| [04 Agents Orchestration](docs/04-agents-orchestration.md) | Planner/Executor, Deep Research loop, Harness² profiles, budget control? |
| [05 Data Model](docs/05-data-model.md) | Schema Postgres/SQLite, cache Redis, vector, retention? |
| [06 API Spec](docs/06-api-spec.md) | Endpoint REST/SSE (`/v1/search`, `/v1/answer`, `/v1/research`, `/v1/digests`, `/v1/engines`), error model? |
| [07 MCP Integration](docs/07-mcp-integration.md) | Tool cho AI Agent, contract, ví dụ gọi? |
| [08 Deployment](docs/08-deployment.md) | Lite profile (không Docker) vs Docker Compose dev 16GB / prod VPS? |
| [09 Observability & Eval](docs/09-observability-eval.md) | Trace/metrics/log, eval 40 golden queries + faithfulness judge? |
| [10 Security & Compliance](docs/10-security-compliance.md) | SSRF IP-pinning, RBAC admin, robots.txt, rate limit, secrets? |
| [11 Testing Strategy](docs/11-testing-strategy.md) | Test strategy, 333 unit tests, golden set, đo citation accuracy? |
| [12 Roadmap & Plan](docs/12-roadmap-plan.md) | **Plan chi tiết Phase 0–5 (v1.0.0), KPI, rủi ro & mitigation** |
| [13 Versioning & Release](docs/13-versioning-and-release.md) | **Quy chuẩn SemVer, Quality Gates, script release tự động & CI/CD** |

## 6. Đứng trên vai người khổng lồ

MetaSearch không phát minh lại bánh xe — thiết kế này tổng hợp tinh hoa từ các hệ thống mã nguồn mở tiêu biểu:

| Nguồn tham khảo | Điều mượn vào MetaSearch |
|---|---|
| [assafelovic/gpt-researcher](https://github.com/assafelovic/gpt-researcher) | Mẫu **Planner & Executor**: tách câu hỏi lớn → sub-queries, gọi song song, tổng hợp có dẫn nguồn; abstraction `retrievers/` + `scraper/` |
| [ItzCrazyKns/Perplexica (Vane)](https://github.com/ItzCrazyKns/Perplexica) | Tích hợp **SearXNG** làm unified search API; lọc đoạn văn trước khi đưa vào LLM; streaming answer |
| [rashadphz/farfalle](https://github.com/rashadphz/farfalle) | Tách lớp sạch (search/scraper/LLM provider), hoán đổi linh hoạt cloud ↔ local LLM |
| [miurla/morphic](https://github.com/miurla/morphic) · [scira-ai/scira](https://github.com/scira-ai/scira) | **Generative UI**: hiển thị kết quả dạng thẻ/bảng, multi-tool calling |
| [searxng/searxng](https://github.com/searxng/searxng) | **Trái tim meta-search**: 70+ engines, khử trùng lặp, consensus ranking, xuất JSON |
| [unclecode/crawl4ai](https://github.com/unclecode/crawl4ai) | Extraction dành riêng cho LLM: loại ads/menu, xuất Markdown, JS rendering |
| [jina-ai/reader](https://github.com/jina-ai/reader) | Fallback extractor cho trang khó (dùng `r.jina.ai` khi self-extract thất bại) |
| [siosig/searxng-crawl4ai-mcp](https://github.com/siosig/searxng-crawl4ai-mcp) | Mẫu ghép SearXNG + Crawl4AI thành **MCP tool duy nhất** `web_search_and_scrape` |
| [chen150450/multi-search-aggregator](https://github.com/chen150450/multi-search-aggregator) | **Engine registry pluggable + hot config**; **multi-signal scoring**; SSE per-engine progress (`engine_done`); graceful degradation |

Điểm **khác biệt** của MetaSearch: orchestration mỏng đứng trên 4 "gã khổng lồ" — **SearXNG** (meta-search), **crawl4ai** (JS extraction), **trafilatura** (static extraction), **FastMCP** (agent interface) — kết hợp cùng tầng **Pháp Y Tài Chính (CAMELS / Altman Z / Beneish M / Piotroski F)** và **Phân Tích Kỹ Thuật Định Lượng (Fibonacci / Ichimoku / MACD / Wilder's RSI & ATR)** chuyên sâu. Chiến lược chi tiết: [ADR-007](docs/adr/ADR-007-giants-first.md).

## 7. License & Đóng góp (Contributions)

Dự án được phát hành dưới giấy phép mã nguồn mở **GNU General Public License v3.0 (GPL-3.0-or-later)**.

Chúng tôi rất hoan nghênh và chào đón mọi sự đóng góp từ cộng đồng (Issues, Feature Requests, Pull Requests)! Vui lòng xem hướng dẫn chi tiết tại [CONTRIBUTING.md](CONTRIBUTING.md) để bắt đầu thiết lập môi trường phát triển, nắm rõ workflow và quy ước kiểm thử chất lượng trước khi gửi đóng góp.

*Lưu ý: Mọi hoạt động thu thập dữ liệu web và tìm kiếm cần tuân thủ `robots.txt` cũng như điều khoản dịch vụ của các nền tảng nguồn (xem thêm tại [docs/10-security-compliance.md](docs/10-security-compliance.md)).*
