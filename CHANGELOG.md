# Changelog

Tất cả thay đổi đáng chú ý của MetaSearch được ghi ở đây.
Format theo [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
version tuân theo [SemVer](https://semver.org/).

## [1.0.0] — 2026-11-01 — Institutional Financial Forensics, Quantitative Technicals & 5-Wave Deep Architectural Audit

### Added
- **Wave 3 — Institutional Financial Forensics (`app.retrieval.financial_forensics`)**:
  - Phân nhánh mô hình pháp y BCTC theo ngành (`industry_type`: `"banking"`, `"securities"`, `"real_estate"`, `"manufacturing"`).
  - **Mô hình CAMELS / Basel II-III cho Ngân hàng** (`VCB`, `TCB`, `MBB`, `ACB`): CAR, Tỷ lệ nợ xấu (NPL), Nợ nhóm 2, Tỷ lệ bao phủ nợ xấu (LLR), NIM, CASA, LDR, CIR.
  - **Mô hình Chứng khoán** (`SSI`): Dư nợ Margin / VCSH (đối chiếu trần pháp lý 200% của UBCKNN), cơ cấu tài sản tài chính `FVTPL` / `HTM` / `AFS`, tỷ lệ an toàn vốn khả dụng (415% vs ngưỡng 180%).
  - **Mô hình Bất động sản & KCN** (`IDC`, `VHM`): Người mua trả tiền trước & Doanh thu chưa thực hiện (Unbilled Bookings / Của để dành), Tỷ trọng tồn kho / Tổng tài sản, Tiền mặt / Nợ ngắn hạn.
  - Bổ sung bộ hàm định lượng tường minh: `calc_altman_z_score`, `calc_beneish_m_score`, `calc_piotroski_f_score` và chuẩn hóa toán học dynamic fallback.
- **Wave 3 — Quantitative Technical Analysis (`app.retrieval.technical_analysis`)**:
  - Bộ đọc tham số chỉ báo tùy chỉnh từ truy vấn tự nhiên `parse_custom_indicator_params(query)` (`RSI 9`, `MACD 8 21 5`, `Bollinger 15 2.5`, `ATR 10`, `Stoch 14 3`).
  - **True EMA-based MACD** (`macd_line`, `signal_line`, `histogram`), **Wilder's smoothed RSI**, **Wilder's ATR & 2x ATR Trailing Stop**, **Stochastic Oscillator (`%K`, `%D`)**, **Multi-EMA Ribbon (`EMA20`, `EMA50`, `EMA200`)**.
  - **Fibonacci Retracement & Extensions**: Vùng **Golden Pocket (`61.8% - 65.0%`)**, mở rộng `127.2%` & `261.8%`, và lọc tách biệt hỗ trợ gần nhất (`< current_price`) / kháng cự gần nhất (`> current_price`).
  - **Ichimoku Kinko Hyo**: Bổ sung tín hiệu `chikou_signal` so sánh với đường giá 26 phiên trước.
  - **Geometric Trendlines & Pattern Recognition**: Kênh hồi quy tuyến tính (`regression_upper`, `regression_lower` $\pm 2\sigma$, hệ số $R^2$) và 8 mẫu hình giá/nến nâng cao (`Double Bottom`, `Double Top`, `Head & Shoulders`, `Ascending Triangle`, `Volume Breakout`, `Bullish Pin Bar`, `Shooting Star`, `Bearish Engulfing`).
- **Wave 4 — Multi-Engine Adapters Hardening (`viet_stock`, `stooq`, `yahoo_finance`, `fengshui`, `wikipedia`, `ddg`, `google_news`)**:
  - Mở rộng `VIET_STOCKS_DB` lên 14 doanh nghiệp đầu ngành HSX/HNX kèm bộ sinh chuỗi OHLCV riêng biệt theo từng mã cổ phiếu `_build_stock_ohlcv_series`.
  - Nâng cấp `fengshui.py` với công thức Cung Phi vạn niên `_digital_root` cho mọi năm sinh `1900..2199`, bổ sung Tam Hợp / Tứ Hành Xung và tra cứu trực tiếp theo tên Can Chi.
- **Wave 5 — Frontend UX/UI & Eval Harness (40 Golden Queries + Faithfulness Judge)**:
  - Hoàn thiện hệ thống CSS trong `frontend/app/globals.css`, thanh chọn chuyên ngành `CATEGORY_CHIPS`, và bộ render bảng biểu Markdown `<table>` trong `frontend/app/page.tsx`.
  - Mở rộng `api/evals/golden.json` lên 40 golden queries và bổ sung `--judge-faithfulness`, `--concurrency` cho `api/scripts/eval_run.py`.

### Fixed
- **Wave 1 — Core Infrastructure, Security, Persistence & Resilience**:
  - Sửa lỗi đệ quy vô tận (`RecursionError`) trên 4 MCP tools trong `app/mcp/server.py`.
  - Bổ sung RBAC `Depends(require_admin)` cho `PATCH /v1/engines/{id}` và `POST /v1/digests/scan`; sửa TTL Redis sliding-window (`window_s + 1`), giới hạn `_key_cache`, gắn header `X-RateLimit-Remaining`, và ghi nhận Prometheus metrics `HTTP_REQUESTS` / `HTTP_LATENCY`.
  - Đồng bộ `DbEngineStore._record_db` (`consecutive_failures` + `_fallback`), sửa lỗi FK `persist_report` (`job.id == research_id` + `_resolve_valid_chunk_ids`), gộp transaction `_persist_search_all`, chống nhân bản `ChunkRow`, và thêm in-memory fallback `_memory_posts` cho `AutomatedScanner`.
  - Tái kiểm tra SSRF `resolve_and_check(target_url)` trước khi leo thang sang `_crawl4ai`/`_jina` sau HTTP redirect; thêm `"Connection": "close"` cho pinned IP request; kiểm tra regex `_LANG_RE` trong `WikipediaAdapter`.
- **Wave 2 — Retrieval, Merger, Scoring & Deep Research**:
  - Bổ sung `infer_categories_from_query` tự động kích hoạt vertical engines khi `categories == ["general"]`.
  - Điền bảng `_TRUSTED_DOMAINS` (`domain_authority`), giữ `snippet_quality = 0.95` cho báo cáo bảng biểu định lượng, nâng `diversity_cap` lên 4 kết quả/domain cho `viet_stock`, `stooq`, `fengshui`, `sec_edgar`.
  - Bảo toàn dữ liệu định lượng trong `AnswerPipeline._score_chunks`, chạy song song `sub_queries` bằng `asyncio.gather` trong `ResearchPipeline`, và giữ tối đa 3 chunks/URL mới trong `ContextPack.merge`.
  - Sửa regex word-boundary `\b` trong `viet_stock._extract_tickers` và `stooq._extract_stooq_symbol` để triệt tiêu hoàn toàn false positive trên từ tiếng Anh thông thường.

## [0.10.0] — 2026-11-01 — Automated Scanning Scheduler, Daily Briefings Blog Engine & Multi-Domain Expansion (17 Engines)

### Added
- **2 Search Engines Mới (Nâng tổng số lên 17 Engines)**:
  - **Google News RSS Adapter (`google_news`)**: Tin tức thời sự cập nhật từng phút qua Google News RSS endpoint keyless, hỗ trợ đa ngôn ngữ và tự động nhận diện tiếng Việt (`hl=vi&gl=VN`), phân tách tự động nguồn báo (VnExpress, CafeF, Tuổi Trẻ,...), zero CAPTCHA.
  - **Feng Shui & Metaphysics Adapter (`fengshui`)**: Công cụ giải toán và tra cứu phong thủy phương Đông: Thiên Can, Địa Chi, 60 Hoa Giáp, Ngũ Hành nạp âm, Bát Trạch Cung Phi (Đông Tứ Mệnh vs Tây Tứ Mệnh), 4 Cát Tinh (Sinh Khí, Diên Niên, Thiên Y, Phục Vị), 4 Hung Tinh, màu sắc tương sinh / tương khắc và bài trí không gian khoa học.
- **Hệ Thống Lịch Quét Tự Động & Daily Briefings / Blog Engine**:
  - Module `AutomatedScanner` (`app.orchestration.scheduler`) tự động thực hiện quét theo chu kỳ hoặc kích hoạt tức thì theo các chủ đề trọng điểm.
  - Bộ REST API v1 tại `/api/v1/digests`: `GET /topics`, `GET /`, `GET /{slug_or_id}`, `POST /scan`.
- **Giao Diện Frontend Web UI Hiện Đại (Tab Navigation & Blog Reader)**:
  - Thanh chuyển đổi giao diện: Tab `🔍 Tìm Kiếm & Hỏi Đáp (Live)` song hành với Tab `📰 Bản Tin & Blog Chuyên Sâu`.

## [0.9.0] — 2026-11-01 — Deep Financial & Equities Intelligence (Yahoo Finance & SEC EDGAR)

### Added
- **2 Financial Vertical Search Engines (Tổng cộng 15 engines)**:
  - **Yahoo Finance Adapter (`yahoo_finance`)**: truy vấn dữ liệu thị trường theo thời gian thực qua endpoints keyless.
  - **SEC EDGAR Adapter (`sec_edgar`)**: truy vấn trực tiếp kho báo cáo tài chính đã kiểm toán của Ủy ban Chứng khoán Hoa Kỳ (`10-K`, `10-Q`, `8-K`, `S-1`).
- **Dedicated Financial Domain Scaffolding trong Research Harness²**:
  - Bổ sung profile `"financial"` trong `HARNESS_PROFILES`.

## [0.8.0] — 2026-10-31 — Universal Engine Expansion & Anti-CAPTCHA Architecture

### Added
- **4 Search Engines mới (Tổng cộng 13 engines)**:
  - **GitHub Adapter (`github`)**, **PubMed Adapter (`pubmed`)**, **HuggingFace Hub Adapter (`huggingface`)**, **Semantic Scholar Adapter (`semanticscholar`)**.
- **Kiến trúc Anti-CAPTCHA & Anti-Bot toàn diện**:
  - Realistic Browser Fingerprint Headers, phát hiện Interstitial Challenge (`is_challenge_page`), tự động leo thang sang Crawl4AI và Jina Reader proxy.

## [0.7.0] — 2026-10-31 — FR-3 Complete, Durable Research & Eval Expansion

### Added
- **Stack Overflow adapter (FR-3 complete)** & **Reddit adapter (FR-3 complete)**.
- **Durable Research Job Recovery**: `ResearchJobStore.get()` tự động phục hồi finished research report và citations từ DB khi server restart.
- **Mở rộng Eval Harness**: `evals/golden.json` mở rộng từ 10 lên 30 queries đa dạng.

### Fixed
- **Unawaited coroutine bug**: chuẩn hóa `persist_report` thành hàm đồng bộ `def`.

## [0.6.0] — 2026-10-31 — Lite profile: chạy không Docker trên máy yếu

### Added
- **DuckDuckGo adapter (keyless)**, **SQLite persistence (Lite profile)**, **In-process fallbacks giữ chất lượng khi không Redis**, **Jina Reader keyless**.

## [0.5.0] — 2026-10-31 — Phase 3: Deep Research & MCP + Phase 4 lõi

### Added
- **Deep Research (FR-9)**, **MCP server (FR-10)**, **Auth + rate limit (FR-14)**, **Vertical engines (FR-3: arXiv + Wikipedia)**, **Serper + Brave adapters (FR-2)**, **Phase 4 lõi (eval harness, Langfuse, OTel)**.

### Fixed
- arXiv Atom parsing qua `defusedxml` (bandit B314/B405 — untrusted XML).
- Research plan/learnings event bị trùng lặp trong SSE (callback + yield).

## [0.4.0] — 2026-10-29 — Observability, operations & performance

### Added
- **Grafana + Prometheus thật**, **Quản lý operations (`GET /v1/ops/status`, `GET /v1/queries`)**, **Common error phía client (`ApiError`)**, **Performance harness (`perf_load.py`, `k6`)**.

### Fixed
- **DB-partition stall**: mọi DB op của `DbEngineStore` giờ bounded `ENGINE_STORE_DB_TIMEOUT_S=0.5`.

## [0.3.1] — 2026-10-29 — Expert audit (3 pass)

### Fixed
- **CORS thiếu hoàn toàn** → thêm `CORSMiddleware` + `CORS_ORIGINS`.
- crawl4ai/jina response cap, answer cache key params, background persist ordering, shared HTTP pool.

## [0.3.0] — 2026-10-27 — Hardening sớm + token counting

### Added
- **Pin-IP connector** (`app/extraction/pinning.py`) & **tiktoken token counting** (`app/llm/tokens.py`).

## [0.2.0] — 2026-10-27 — Phase 1: MVP Search

### Added
- `POST /v1/search`, `POST /v1/answer` (JSON + SSE), `POST /v1/scrape`, `GET /v1/engines`, CLI `metasearch search/answer`.

## [0.1.0] — 2026-10-26 — Phase 0: Scaffold (giants-first)

### Added
- Monorepo `api/` (FastAPI) + `frontend/` (Next.js 15) + `deploy/` + `docs/` (12 tài liệu thiết kế + 8 ADR).
