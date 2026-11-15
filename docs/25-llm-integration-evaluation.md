# Docs/25 — Đánh giá & Tích hợp Hệ thống AI Infrastructure (v3.2)

> **Mục tiêu**: Đánh giá, lựa chọn pattern kiến trúc và tích hợp trực tiếp (in-repo, zero-sidecar cho Lite profile) các khả năng cốt lõi từ 4 dự án tham chiếu (`OmniRoute`, `headroom`, `one-skill-to-rule-them-all`, `claude-mem`) vào runtime của MetaSearch, kèm kiểm chứng bảo mật và test E2E.
>
> **Ngày hoàn tất**: 2026-11-15  
> **Trạng thái**: Đã triển khai trong codebase (`api/app/llm/`, `api/app/orchestration/`, `api/app/retrieval/memory.py`, `api/app/mcp/server.py`)

---

## 1. Bảng tổng hợp 4 trụ cột tích hợp

| Dự án tham chiếu | Vai trò trong MetaSearch | Module triển khai thực tế (Native In-Repo) | Cách tích hợp (Lite-safe) |
|---|---|---|---|
| **[OmniRoute](https://github.com/cheaper-inference/OmniRoute)** | Kiểm định API key đa provider, health tracking & xếp hạng failover thông minh | [key_validator.py](../api/app/llm/key_validator.py), [router.py](../api/app/llm/router.py), [providers.py](../api/app/llm/providers.py) | Không cần gateway ngoài; tích hợp thẳng vào `LLMGateway` + API `/v1/llm/validate` & `/v1/llm/health`. |
| **[headroom](https://github.com/headroomlabs-ai/headroom)** | Nén ngữ cảnh theo cấu trúc & trích xuất nguyên văn, ổn định prefix cho KV-cache | [compressor.py](../api/app/llm/compressor.py), hook vào [pipeline.py](../api/app/orchestration/pipeline.py) | `ContentRouter` + `SmartCrusher` + `SearchCompressor` + `CacheAligner`; giữ nguyên URL/con số trích dẫn. |
| **[one-skill-to-rule-them-all](https://github.com/rebelytics/one-skill-to-rule-them-all)** | Định tuyến kỹ năng thống nhất (9 skills) & theo dõi chất lượng thực thi (`TaskObserver`) | [skill_orchestrator.py](../api/app/orchestration/skill_orchestrator.py), [skills.py](../api/app/api/v1/skills.py) | Bọc `DomainIntelligenceEngine`, điều khiển `compression_profile` và ghi telemetry giới hạn bộ nhớ. |
| **[claude-mem](https://github.com/thedotmack/claude-mem)** | Trí nhớ dài hạn theo API key (`kind="observation"`) & Progressive Disclosure 3 lớp | [memory.py](../api/app/retrieval/memory.py), [memory.py API](../api/app/api/v1/memory.py), [mcp/server.py](../api/app/mcp/server.py) | Mở rộng bảng `memory_items` có sẵn + MCP tools `memory_search` / `memory_timeline` / `memory_get_observations`. |

> **Lưu ý kiến trúc**: Toàn bộ 4 khối đều chạy **nội tuyến (in-process)** trong `api/`, không bắt buộc chạy container/sidecar phụ nên giữ nguyên tương thích **Lite zero-Docker profile**.

---

## 2. Chi tiết triển khai từng khối

### 2.1 OmniRoute-inspired Multi-LLM Validator & Health-Aware Router

- **Kiểm định định dạng & Live Probe ([key_validator.py](../api/app/llm/key_validator.py))**:
  - `KEY_FORMAT_SPECS`: định nghĩa 17 đặc tả format key theo từng preset (`openrouter`, `gemini`, `groq`, `nvidia_nim`, `cerebras`, `sambanova`, `mistral`, `github_models`, `cloudflare`, `cohere`, `chutes`, `siliconflow`, `hyperbolic`, `huggingface`, `openai`, `anthropic`...), kèm nhận diện placeholder (`changeme`, `your_api_key`, `sk-test`...).
  - `validate_key_format(provider, api_key)` từ chối key chứa whitespace, ký tự điều khiển (`\r`, `\n`), hoặc ký tự non-ASCII trước khi đưa vào HTTP header.
  - `validate_provider_key()` dùng HTTP pool dùng chung `get_http_client("internal")` (chỉ tự tạo client khi test truyền `client=` riêng), bảo vệ SSRF (`validate_llm_base_url(..., allow_private=(target.provider == "ollama"))`), hỗ trợ `dry_run=True`, probe đúng endpoint xác thực (ví dụ OpenRouter dùng `/auth/key` thay vì `/models` không cần auth), và mask URL/key an toàn (`_sanitize_base_url_for_display`, `mask_api_key`).
  - `validate_all_configured_providers()` giới hạn đồng thời bằng `asyncio.Semaphore(6)` và cô lập `runtime_override` đúng provider qua `scoped_runtime_override()` (tránh rò credential sang provider khác).
- **Xếp hạng & Failover ([router.py](../api/app/llm/router.py), [gateway.py](../api/app/llm/gateway.py))**:
  - `ProviderHealthTracker` lưu trạng thái probe (`key_valid`, `reachable`, `http_status`, `last_error`) kết hợp EWMA latency và circuit breaker (`llm_breakers`).
  - `score_provider()` tính điểm ưu tiên (càng thấp càng tốt) theo: trạng thái cấu hình/breaker, độ tin cậy key, tier miễn phí (`permanent_free`, `free_keyless`, `free_rate_limited`, `local_free`, `trial_credits`, `paid`), EWMA latency và `LLM_FALLBACK_CHAIN`.
  - `LLM_ALLOW_KEYLESS_FALLBACK` mặc định `false`: không tự ý rơi sang `pollinations` nếu operator chưa bật rõ ràng khi provider có key bị lỗi.
- **Endpoints**:
  - `POST /v1/llm/validate` (yêu cầu `require_admin` khi `AUTH_ENABLED=true`): kiểm định hàng loạt hoặc danh sách `providers`, hỗ trợ `dry_run`.
  - `GET /v1/llm/health?task=answer|report`: xem xếp hạng định tuyến `route_summary` kèm `runtime_override`.

### 2.2 Headroom-inspired Context Compression & KV-Cache Alignment

- **Định tuyến nội dung ([compressor.py](../api/app/llm/compressor.py))**:
  - `ContentRouter.classify()` phân loại chunk thành `json`, `markdown_table`, `code_log`, hoặc `prose`.
  - `SmartCrusher`: tỉa JSON rỗng/mảng dài giữ phần tử khớp truy vấn; rút gọn bảng Markdown giữ header, tổng kết và các dòng liên quan; bảo toàn block mã nguồn (`code_log`).
  - `SearchCompressor`: trích xuất câu nguyên văn có tỷ trọng truy vấn + tín hiệu số liệu/thời gian (`factual_weight`), **không tự bịa hay viết lại câu**.
  - 4 compression profile qua `_PROFILE_PARAMS`: `balanced`, `preserve_numbers`, `preserve_code`, `compact` (do `SkillSelection.compression_profile` điều phối).
- **Ổn định prefix & tách ranh giới memory**:
  - `CacheAligner.align_messages()` đặt system prompt bất biến ở đầu, tính hash prefix để theo dõi tái sử dụng KV-cache, và bọc lịch sử nhớ lại (`recalled_memory`) trong thẻ phân tách ngẫu nhiên `<<<PRIOR_MEMORY_{tag}>>>` nhằm giảm rủi ro prompt injection từ nội dung nhớ cũ.
- **Tích hợp pipeline**: Bật qua `CONTEXT_COMPRESSION_ENABLED=true`, kích hoạt khi số chunk >= `CONTEXT_COMPRESSION_MIN_CHUNKS` (`2`). Payload `POST /v1/answer` và `POST /v1/context` (`include` hỗ trợ thêm `"compression"`) trả về thống kê tiết kiệm token (`CompressionStats`).

### 2.3 One-Skill-to-Rule-Them-All & TaskObserver Telemetry

- **Registry 9 Skill ([skill_orchestrator.py](../api/app/orchestration/skill_orchestrator.py))**:
  - `list_skills()` trả về 9 skill hợp nhất (`financial_forensics`, `biomedical_clinical`, `technology_engineering`, `academic_synthesis`, `legal_macro_policy`, `eastern_metaphysics`, `news_geopolitics`, `general_grounded_search`, `deep_research_synthesis`), chuẩn hóa tên miền từ `DomainIntelligenceEngine`, chọn `compression_profile`, chế độ rerank và budget ngữ cảnh.
- **Telemetry giới hạn bộ nhớ**:
  - `TaskObserver` ghi nhận độ trễ, chế độ nén, số câu không được chứng minh (`unsupported_markers`) theo từng skill vào ring buffer tối đa 200 sự kiện/skill.
- **Endpoints**:
  - `GET /v1/skills` (tùy chọn `?q=...&deep=...`): liệt kê skills và xem trước quyết định routing.
  - `GET /v1/ops/skills` (admin): xem thống kê thực thi từ `TaskObserver`.
  - `POST /v1/context` hỗ trợ `"skill"` trong `include`.

### 2.4 Claude-Mem-inspired Long-Term Episodic Memory

- **Lưu trữ & thu thập ([memory.py](../api/app/retrieval/memory.py))**:
  - Mở rộng `memory_items` với `kind="observation"`, `distill_answer_observation()` chưng cất câu trả lời thành tóm tắt ngắn có `title`, `target`, `tags`, `url` qua bounded persist queue (`record_observation_fire_and_forget`). Toàn bộ dữ liệu cách ly theo `api_key_id`.
- **Progressive Disclosure 3 lớp (REST + MCP)**:
  1. `GET /v1/memory/search` (MCP tool `memory_search`): chỉ mục gọn (ID, tiêu đề, tags, timestamp).
  2. `GET /v1/memory/timeline` (MCP tool `memory_timeline`): cửa sổ ngữ cảnh theo thời gian quanh `anchor_id` (truy vấn index trực tiếp quanh anchor, không bị cắt ngưỡng 500 dòng).
  3. `POST /v1/memory/observations` (MCP tool `memory_get_observations`): tải toàn văn các ID đã chọn.
- **Recall vào pipeline**: `recall_context_for_query()` chèn ký ức liên quan trong ngưỡng `MEMORY_RECALL_MAX_TOKENS=250` khi `MEMORY_RECALL_ENABLED=true`.

---

## 3. Biến môi trường cấu hình

Xem đầy đủ trong [deploy/.env.example](../deploy/.env.example) và [deploy/.env.lite.example](../deploy/.env.lite.example):

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `LLM_AUTO_FALLBACK` | `true` | Bật/tắt tự động failover sang provider dự phòng. |
| `LLM_FALLBACK_CHAIN` | `""` | Danh sách provider dự phòng ưu tiên (phân tách dấu phẩy). |
| `LLM_ALLOW_KEYLESS_FALLBACK` | `false` | Cho phép fallback sang provider không cần key khi provider chính cần key lỗi. |
| `LLM_KEY_PROBE_TIMEOUT_S` | `5.0` | Timeout probe xác thực API key (`0.5`–`30.0` giây). |
| `CONTEXT_COMPRESSION_ENABLED` | `true` | Bật/tắt nén ngữ cảnh Headroom trước khi gọi LLM. |
| `CONTEXT_COMPRESSION_MIN_CHUNKS` | `2` | Số chunk tối thiểu mới chạy nén (`1`–`50`). |
| `SKILL_ORCHESTRATION_ENABLED` | `true` | Bật/tắt One-Skill router & `TaskObserver`. |
| `MEMORY_RECALL_ENABLED` | `true` | Bật/tắt tự động recall observation theo `api_key_id`. |
| `MEMORY_RECALL_MAX_TOKENS` | `250` | Ngân sách token tối đa cho ngữ cảnh ký ức chèn thêm (`0`–`2000`). |

---

## 4. Ràng buộc bảo mật, Benchmark Nén & Kiểm chứng

### 4.1 Benchmark định lượng nén ngữ cảnh (`F-21`)

Kiểm thử đối chứng trong `tests/test_w5_guardrails.py::test_compression_profiles_preserve_citations_and_numeric_facts` đo trực tiếp 4 profile của `SearchCompressor` trên tập chunk tài chính + mã nguồn (ngân sách mục tiêu `budget_tokens=160`):

| Profile | Vai trò chính (`SkillOrchestrator`) | Tỷ lệ giữ câu có số liệu (`15.920`, `28,1%`) | Bảo toàn khối mã nguồn (`code_log`) | Bảo toàn danh sách chunk & URL trích dẫn `[n]` |
|---|---|---|---|---|
| `balanced` | Mặc định đa lĩnh vực | **100%** | Giữ nguyên khối code khớp truy vấn | **100%** (`2/2` chunk) |
| `preserve_numbers` | `financial_forensics`, `biomedical_clinical` | **100%** (ưu tiên trọng số số liệu `factual_weight`) | Giữ nguyên | **100%** (`2/2` chunk) |
| `preserve_code` | `technology_engineering` | **100%** | **100%** (`def calculate_roe` nguyên vẹn) | **100%** (`2/2` chunk) |
| `compact` | `news_geopolitics`, tóm tắt nhanh | **100%** | Giữ nguyên | **100%** (`2/2` chunk) |

### 4.2 Kiểm chứng an toàn & cổng chất lượng (`F-20`)

- **Kiểm định API key độc lập môi trường**: `POST /v1/llm/validate` kiểm tra 17 đặc tả format (`KEY_FORMAT_SPECS`), chặn ký tự điều khiển/header injection và thực thi probe qua `get_http_client("internal")` có SSRF guard (`validate_llm_base_url`). Trên môi trường mặc định chưa cấu hình key (`LLM_PROVIDER=none`), toàn bộ 17 preset cần key được báo cáo rõ trạng thái `unconfigured`, không ngụy tạo kết quả probe.
- **Bảo vệ quyền truy cập**: `POST /v1/llm/validate` yêu cầu `require_admin`, và `GET /v1/llm/health` yêu cầu `require_operator` (chặn key `role="user"` trong môi trường multi-tenant `AUTH_ENABLED=true`, vẫn mở cho single-operator Lite `AUTH_ENABLED=false`).
- Chi tiết kiểm thử đơn vị, contract OpenAPI `/v1` (`api/tests/contract/openapi_v1.json`) và toàn bộ cổng chất lượng (`ruff`, `mypy`, `bandit`, `pytest`, `vitest`, `tsc`, `next build`) được kiểm tra đồng bộ trong [tests/test_phase4_ai_infra.py](../api/tests/test_phase4_ai_infra.py), [tests/test_w5_guardrails.py](../api/tests/test_w5_guardrails.py) và ghi nhận tại [CHANGELOG.md](../CHANGELOG.md).
