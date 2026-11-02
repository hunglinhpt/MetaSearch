# Changelog

Tất cả thay đổi đáng chú ý của MetaSearch được ghi ở đây.
Format theo [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
version tuân theo [SemVer](https://semver.org/).

## [1.3.0] — 2026-11-02 — DeepSeek Super-Agent Architecture (Router-Worker, Observe Mode, Quiet Autonomy & Watchdog Supervisor)

### Added
- **Kiến Trúc Autonomous Super-Agent Engine (`app.orchestration.superagent`)**:
  - Tích hợp nguyên mẫu tự trị từ **`deepseek-ai/dsh-superagent-prototype`** (bài báo *"Exploring Super-agent Design: Towards a More Autonomous Agent System"*), giải phóng điểm nghẽn giới hạn chú ý của con người.
  - **Router - Worker Decoupling (`SuperAgentRouter`, `IsolatedSuperAgentWorker`)**: Tách biệt tầng điều phối trung tâm khỏi các phiên làm việc độc lập; Router nhận diện sự kiện, phân rã mục tiêu thành các **SuperAgent Tickets** theo độ ưu tiên (`CRITICAL`, `HIGH`, `NORMAL`, `LOW`).
  - **Triết Lý "Quiet Autonomy" & Escalation Queue**: Agent thực thi tự trị đa bước ReAct mà không làm phiền nhóm; chỉ lên tiếng khi có thành phẩm (`SuperAgentArtifact`) kèm trích dẫn `[n]` hoặc khi phát sinh rủi ro cao cần con người phê duyệt (`EscalationRequest`).
  - **Observe Mode (Shadow Evaluation)**: Chế độ shadow plan thẩm định mục tiêu và phân tích rủi ro mà không tiêu tốn quota live API.
  - **Proactive Event Ingestion (`EventIngestionEngine`, `POST /v1/superagent/events/ingest`)**: Tự động kích hoạt tickets từ các luồng sự kiện thời gian thực (BCTC kiểm toán, thông báo chứng khoán, CVE bảo mật).
  - **Watchdog Supervisor & Circuit Breaker (`WatchdogSupervisor`, `GET /v1/superagent/watchdog/status`)**: Giám sát nhịp tim worker, tự động ngắt mạch (trip circuit) khi vượt ngưỡng thời gian (120s) hoặc bùng nổ token (`TOKEN_BURST`).
  - **Giao Diện Super-Agent UI**: Tab giao diện mới trên Web UI Next.js cho phép submit mục tiêu tự trị, theo dõi tickets, giám sát Watchdog và phê duyệt Escalation trực quan.
  - **Bộ Kiểm Thử Toàn Diện (`api/tests/test_superagent_engine.py`)**: 8 bài kiểm thử chuyên sâu cho Router, Worker, Observe Mode, Quiet Autonomy, Watchdog và REST API (tổng 383+ tests xanh 100%).

## [1.2.1] — 2026-11-02 — Multi-Platform Matrix CI (Windows, macOS, Linux), 3-Round Deep Audit & Hardened Council

### Added
- **Multi-Platform Matrix CI Trên Mọi Commit (`.github/workflows/ci.yml`)**:
  - Mở rộng toàn bộ quy trình CI chạy ma trận tự động trên cả 3 hệ điều hành: **Ubuntu Linux (`ubuntu-latest`)**, **Windows Server (`windows-latest`)**, và **macOS (`macos-latest`)**.
  - Kích hoạt trên **mọi commit** push lên bất kỳ nhánh nào (`branches: ["**"]`) và mọi pull request.
  - Phân tách thành 4 job kiểm định độc lập và toàn diện:
    1. `backend-matrix` (Linux, Windows, macOS): Cưỡng chế Ruff format, Ruff lint, Bandit AST security scan, Mandatory End-to-End full system verification suite (`test_e2e_full_system.py`), và Pytest full suite.
    2. `frontend-matrix` (Linux, Windows, macOS): Cưỡng chế TypeScript strict typecheck (`npm run typecheck`) và Next.js production build (`npm run build`).
    3. `release-pipeline-gate` (Linux, Windows, macOS): Chạy toàn bộ pipeline kiểm định phát hành `scripts/release.py --check` trong môi trường sạch.
    4. `docker-build-smoke` (Linux): Kiểm thử đóng gói Docker container image độc lập cho cả API và Frontend.
- **Cross-Platform Pre-Commit Hook Hardening (`.githooks/pre-commit`, `scripts/release.py`)**:
  - Hỗ trợ phát hiện thông minh môi trường thực thi trên macOS/Linux (`api/.venv/bin/python`, `python3`, `python`) và Windows (`api/.venv/Scripts/python.exe`).
  - Tự động cấp quyền thực thi `chmod +x` cho `.githooks/pre-commit` trên Unix/macOS và đồng bộ index mode 100755 trong Git.
  - Loại bỏ hoàn toàn cờ `shell=True` trong `run_command` của `scripts/release.py` (0 cảnh báo Bandit B602).
- **Bộ Kiểm Thử Sâu Edge-Case Hội Đồng & Replay World (`api/tests/test_council_and_dream_rsi_deep.py`)**:
  - Kiểm thử trích xuất JSON ngoặc nhọn cân bằng từ mọi định dạng phản hồi phức tạp, markdown code blocks, prose bao quanh.
  - Kiểm thử độ ổn định giải thuật UCB1 khi số lần thử ban đầu là 0 hoặc lớn.

### Fixed & Hardened (3-Round Deep Audit)
- **Thuật Toán Trích Xuất JSON Cân Bằng (`api/app/llm/council.py`)**: Thay thế regex greedy `r"\{.*\}"` bằng balanced braces extraction parser và markdown fence cleaner, ngăn chặn triệt để lỗi parse khi LLM chèn prose hoặc code block.
- **Xử Lý Điểm 0.0 Không Bị Đổi Thành Điểm Fallback (`api/app/llm/council.py`)**: Sửa lỗi falsy evaluation `item.get("score") or 7.5` khiến điểm 0.0 của model bị gán thành 7.5; bọc try/except an toàn riêng từng ứng viên.
- **Tính Đồng Thuận Pairwise Jaccard Chuẩn Xác (`api/app/llm/council.py`)**: Cải tiến `_compute_consensus_agreement` sang trung bình Jaccard theo từng cặp ứng viên (Pairwise Jaccard), loại bỏ sai lệch khi có 1 model lệch nguồn.
- **Cô Lập Timeout Từng Endpoint Hội Đồng (`api/app/llm/council.py`)**: Thiết lập timeout 40s độc lập cho mỗi panelist; một endpoint bị treo không làm nghẽn toàn bộ hội đồng.
- **Dọn Dẹp Coroutine Khi Client Ngắt Kết Nối SSE (`api/app/api/v1/answer.py`)**: Bắt `asyncio.CancelledError` và await task cancellation với `contextlib.suppress`, triệt tiêu cảnh báo rò rỉ tài nguyên.
- **Gia Cố Che Giấu Khóa API Ngắn (`api/app/llm/providers.py`)**: Nâng cấp `mask_api_key` với các key độ dài 8-16 ký tự để tránh brute-force.

## [1.2.0] — 2026-11-02 — Multi-Free-Endpoint LLM Council, Dream-RSI Replay World, Directory Integration (nejib1/Free-LLM & awesome-freellm-apis) & Universal Multi-Domain Intelligence

### Added
- **Multi-Free-Endpoint LLM Council & Anonymous Cross-Review (`app.llm.council`, `POST /v1/llm/council`, `PATCH /v1/llm/council/endpoints`)**:
  - Hỗ trợ gọi song song đồng thời nhiều Free LLM endpoints trong 1 lượt truy vấn (mặc định OpenRouter Free, Pollinations keyless, Groq Free, Google Gemini Free).
  - Cơ chế **Anonymous Cross-Review**: làm mù danh tính các model thành `Candidate A, B, C...` để các panelist chấm chéo lẫn nhau theo thang rubric chuyên ngành mà không bị thiên kiến tự chấm điểm cao cho chính mình.
  - Kết hợp chấm điểm tất định (`deterministic_score` dựa trên độ chuẩn xác trích dẫn `[n]`, độ phủ nguồn context, và mật độ dữ liệu định lượng — trọng số 40%) với điểm bình duyệt đồng cấp từ LLM (`peer_score` — trọng số 60%) để chọn ra Candidate chiến thắng và giao Chủ tịch Hội đồng (`Chairman`) tổng hợp phán quyết tối hậu (`verdict_md`).
  - Khả năng **Graceful Degradation**: tự động loại bỏ endpoint bị lỗi 429 hoặc timeout mà không làm hỏng cả hội đồng.
- **Dream-RSI (Recursive Self-Improvement via Replay World) (`app.llm.council`, `GET /v1/llm/dream-rsi/state`, `POST /v1/llm/dream-rsi/reset`)**:
  - Lưu trữ lịch sử phán quyết vào **Replay Buffer** (`ReplayTrajectory`), tính toán điểm tiềm năng khai phá **Upper Confidence Bound (UCB1)** cho từng endpoint.
  - Tự động chạy **Dream Replay & Optimization** sau mỗi hội đồng: mô phỏng lại các truy vấn lịch sử để tự động tiến hóa chính sách chọn lựa panel tốt nhất (`recommended_panel`), tính độ lệch cải thiện qua các thế hệ (`replay_improvement_delta`) và điều chỉnh nhiệt độ tối ưu (`optimal_temperature`) theo từng miền chuyên sâu.
- **Tích Hợp Danh Mục Free LLM Toàn Diện (nejib1/Free-LLM & awesome-freellm-apis)**:
  - Tích hợp 2 danh mục Free LLM mở rộng từ [`nejib1/Free-LLM`](https://github.com/nejib1/Free-LLM) (34+ Free LLM APIs) và [`open-free-llm-api/awesome-freellm-apis`](https://github.com/open-free-llm-api/awesome-freellm-apis) (134+ Free LLM APIs).
  - Mở rộng lên **20 LLM provider presets**: `openrouter`, `pollinations`, `gemini`, `groq`, `nvidia_nim`, `cerebras`, `sambanova`, `mistral`, `github_models`, `cloudflare`, `cohere`, `chutes`, `siliconflow`, `hyperbolic`, `huggingface`, `ollama`, `custom`, `openai`, `anthropic`, `none`.
  - Phân loại rõ ràng thuộc tính gói cước (`keyless_free`, `permanent_free`, `trial_credits`, `local_free`, `paid_or_byo`).
- **Kiến Trúc Universal Multi-Domain Deep Intelligence (`app.retrieval.domain_intelligence`, `GET /v1/domains`, `POST /v1/domains/analyze`)**:
  - Mở rộng khả năng thẩm định bản chất chuyên sâu ("hiểu bản chất thay vì chỉ đọc bề mặt") sang đa lĩnh vực, với **Tài chính & Pháp y BCTC (`financial`)** giữ vai trò **Flagship Pilot** thử nghiệm chuyên sâu cao nhất (`flagship_pilot`).
  - Bổ sung 7 gói chuyên sâu tích hợp sẵn:
    1. `financial` (CAMELS, Margin/FVTPL, Unbilled Bookings, Altman/Beneish/Piotroski, Fibonacci Golden Pocket, Ichimoku, Linear Regression Channel $\pm 2\sigma$, 8 Chart Patterns).
    2. `biomedical_health` (PICO Clinical Framing, Evidence Hierarchy, Mechanism of Action, Safety & Contraindications).
    3. `technology_ai` (System Architecture, Algorithmic Complexity $O(\cdot)$, OWASP/CWE Security Auditing, Performance Benchmarks).
    4. `academic_research` (First-Principles Formulation, Dataset & Ablation Validity, Consensus vs Contradictions).
    5. `legal_macro_policy` (Transmission Channels, Regulatory Hierarchy, Sectoral & Macro Impact).
    6. `eastern_metaphysics` (Can Chi, Ngũ Hành, Bát Trạch Cung Phi, Ergonomics kiến trúc khoa học).
    7. `news_geopolitics` (Chronological Timeline, Multi-Source Cross-Verification, Geopolitical Implications).
    8. `general_discovery` (Universal Multi-Engine Synthesis).
- **Cổng Kiểm Định Pre-Commit Bắt Buộc & Toàn Trình E2E Test Suite (`scripts/release.py`, `.githooks/pre-commit`, `tests/test_e2e_full_system.py`)**:
  - Xây dựng bộ test **End-to-End Full System Verification Suite** (`test_e2e_full_system.py`) kiểm tra toàn bộ 20 presets, 8 domains, Council đa endpoint, Cross-review, Dream-RSI, Answer JSON & SSE, Context-Only RAG, và Citation Validator.
  - Cài đặt Git hook `.githooks/pre-commit` (kèm kích hoạt tự động `git config core.hooksPath .githooks`): **Nghiêm cấm mọi lệnh `git commit` nếu bộ kiểm thử Unit hoặc E2E có bất kỳ lỗi nào**.
- **Nâng Cấp Giao Diện Web UI (`frontend/app/page.tsx`, `frontend/lib/api.ts`)**:
  - Nút bật tắt nhanh **`⚖️ Hội Đồng LLM + Dream-RSI`** ngay trên thanh tìm kiếm.
  - Thanh chọn **Miền Chuyên Sâu (Universal Intelligence)** hỗ trợ cả tự động nhận diện và chọn thủ công.
  - Khung hiển thị **Biên Bản Hội Đồng Phán Quyết** trực quan (Chairman Model, Điểm đồng thuận, bảng điểm định lượng và Peer Review của từng Candidate A/B/C).

## [1.1.0] — 2026-11-02 — OpenRouter Free Router (`openrouter/free`), Reputable Free LLM Presets & External System LLM Integration

### Added
- **OpenRouter Free Router (`openrouter/free`) & Multi-Provider Free LLM Registry (`app.llm.providers`, `app.llm.gateway`)**:
  - Tích hợp và cấu hình sẵn **OpenRouter Free Models Router** (`openrouter/free` — `https://openrouter.ai/openrouter/free`, endpoint `https://openrouter.ai/api/v1`) với chi phí `$0/M` input & output, tự động định tuyến tới các model miễn phí tốt nhất đang online (`Llama 3.3 70B`, `DeepSeek V3/R1`, `Gemini 2.0 Flash`, `Qwen 2.5 72B`, `Gemma 3 27B`) kèm chuỗi model dự phòng tự động (`OPENROUTER_FALLBACK_MODELS`).
  - Bổ sung danh mục 13 preset LLM (`LLM_PROVIDER_PRESETS`) bao gồm các nguồn miễn phí uy tín:
    - `openrouter` (`openrouter/free`)
    - `pollinations` (Pollinations AI — Keyless 100% miễn phí, không cần API key)
    - `gemini` (Google Gemini AI Studio Free Tier — `gemini-2.0-flash`, `gemini-2.5-flash`)
    - `groq` (Groq Cloud LPU Free Tier — `llama-3.3-70b-versatile`, `deepseek-r1-distill-llama-70b`)
    - `cerebras` (Cerebras Wafer-Scale Free Tier — `llama-3.3-70b`)
    - `mistral` (Mistral La Plateforme Free Experiment Tier — `mistral-small-latest`)
    - `github_models` (GitHub Models Azure Inference Free — `gpt-4o-mini`, `Llama-3.3-70B-Instruct`)
    - `huggingface` (HuggingFace Serverless Inference Router — `Qwen/Qwen2.5-72B-Instruct`)
    - `ollama` (Local Self-Hosted Ollama / vLLM / LM Studio / llama.cpp — `http://127.0.0.1:11434/v1`)
    - `custom` (BYO-LLM / Internal Enterprise LLM Proxy — LiteLLM, Kong, Azure, vLLM Cluster kèm `CUSTOM_LLM_HEADERS`)
    - `openai`, `anthropic`, `none`.
  - Hỗ trợ cơ chế **Cross-Provider Automatic Fallback (`LLM_FALLBACK_PROVIDER`)**: tự động chuyển sang provider dự phòng (ví dụ `pollinations` keyless free) khi provider chính thiếu API key hoặc gặp lỗi 429/5xx.
- **Hot-Configuration API & Connectivity Probe (`/v1/llm/*`)**:
  - `GET /v1/llm/providers`: Liệt kê toàn bộ 13 provider presets kèm trạng thái cấu hình và danh sách model khuyến nghị.
  - `GET /v1/llm/config` & `PATCH /v1/llm/config`: Xem (đã che mặt nạ API key) và cập nhật nóng `provider`, `protocol`, `base_url`, `api_key`, `quick_model`, `report_model`, `fallback_provider`, `extra_headers` ngay trong lúc chạy mà không cần khởi động lại server.
  - `POST /v1/llm/config/reset`: Khôi phục cấu hình LLM về mặc định `.env`.
  - `POST /v1/llm/test`: Kiểm tra kết nối và đo độ trễ (`latency_ms`) tới bất kỳ endpoint LLM nào.
- **Tích Hợp 2 Chiều Với Hệ Thống Khác Đã Có Sẵn LLM (`app.api.v1.llm`, `app.orchestration.pipeline`)**:
  - **Chiều 1 — External LLM Orchestrator Mode (Context-Only RAG + Citation Validator)**:
    - `POST /v1/context`: Chạy toàn bộ pipeline 19 Search Engines + Smart Category Routing + Pháp y BCTC + Kỹ thuật định lượng + Scrape + Chunk & Rerank để trả về `context_md` (đánh số `[1]..[n]`), `system_prompt`, `messages`, `chunks`, `sources` cho hệ thống ngoài tự gọi LLM nội bộ của họ.
    - `POST /v1/citations/validate`: Nhận `answer_md` do LLM của hệ thống ngoài sinh ra cùng `chunks` để hậu kiểm, lọc bỏ citation giả mạo và trả về `validated_answer_md` + `citations` chuẩn xác.
  - **Chiều 2 — Per-Request & Server-Wide BYO-LLM Override**:
    - Cho phép truyền trực tiếp đối tượng `llm` (`LLMOverride`) trong body của `POST /v1/answer` và `POST /v1/research`, hỗ trợ cả các internal proxy trả về `application/json` lẫn `text/event-stream` kèm bộ chặn SSRF (`validate_llm_base_url`).
- **Giao Diện Cấu Hình LLM & Tích Hợp Hệ Thống Ngoài trên Web UI (`frontend/app/page.tsx`)**:
  - Bổ sung Tab `🤖 Cấu Hình LLM (OpenRouter Free / BYO-LLM)` trên Web UI cho phép chọn nhanh preset, đổi model bằng 1 click, nhập API key / Custom Base URL / Custom Headers, bấm Test kết nối trực tiếp, và xem mẫu code tích hợp `/v1/context`.

## [1.0.0] — 2026-11-01 — Institutional Financial Forensics, Quantitative Technicals & 5-Wave Deep Architectural Audit

### Added
- **Automated Version Management, Quality Gates & Release Pipeline (`scripts/release.py`)**:
  - Xây dựng hệ thống phát hành tự động hóa chuẩn mực: tự động dọn dẹp file tạm/cache (`clean_workspace`), cưỡng chế 6 cổng kiểm định chất lượng (`ruff`, `bandit`, `pytest`, `npm run typecheck`, `npm run build`), đồng bộ phiên bản nguyên tử giữa `api/app/__init__.py`, `api/pyproject.toml` và `frontend/package.json`.
  - Tự động kiểm tra và trích xuất release notes từ `CHANGELOG.md`, đóng gói Git commit (`release: vX.Y.Z`) và annotated Git tag.
  - Tích hợp GitHub Actions CI/CD release workflow (`.github/workflows/release.yml`) tự động tạo GitHub Release chính thức khi push tag.
  - Tài liệu quy chuẩn chi tiết tại `docs/13-versioning-and-release.md`.
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
