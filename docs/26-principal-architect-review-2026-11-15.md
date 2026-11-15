# 26 — Principal Architect Review (2026-11-15)

> **Mục đích**: ghi lại các finding của đợt review kiến trúc + bảo mật + vận hành để chủ dự án và agent khác **đánh giá, phản biện, rồi xử lý**. File này **chỉ là báo cáo**: review này không sửa code nào.
>
> **Phạm vi**: `api/app/` (FastAPI + SQLAlchemy), cấu hình, CI, tài liệu, trạng thái git. Frontend chỉ kiểm cổng build/test, không review sâu.
>
> **Quy ước bằng chứng**: mỗi finding ghi `file:dòng` đã đọc trực tiếp trong phiên review này. Mục nào **chưa kiểm chứng** thì ghi rõ ở §6. Số dòng có thể lệch nếu code đổi sau 2026-11-15, nên agent xử lý cần mở lại file trước khi sửa.

---

## 0. Cách dùng file này (cho agent xử lý)

1. Chọn finding theo thứ tự ưu tiên ở §4. Mở lại code tại `file:dòng` để xác nhận còn đúng.
2. Nếu **không đồng ý** với finding, ghi lý do vào cột *Ghi chú* của §5, đừng xóa finding.
3. Mỗi fix cần: test đi qua route thật (xem mục *Nghiệm thu* của finding), cập nhật `api/tests/contract/openapi_v1.json` nếu đổi hợp đồng (`METASEARCH_UPDATE_OPENAPI=1`), rồi ghi vào `docs/15-integration-guide.md` §9.
4. **Không sửa** `api/tests/test_w2_b_data.py` (ràng buộc có sẵn của dự án).
5. Cập nhật cột *Trạng thái* ở §5 khi xong.

---

## 1. Baseline đã đo (sự kiện, không phải ý kiến)

| Hạng mục | Kết quả quan sát được |
|---|---|
| Backend full `pytest tests -q` | Chạy tới `[100%]`, exit code `0`. Có 8 ký tự `s` (skipped) trong log. Dòng tổng kết `N passed` **không** nằm trong log task. `pytest --collect-only` báo `1032 tests collected`. |
| `tests/test_phase4_ai_infra.py` + `tests/contract/test_openapi_snapshot.py` | `15 passed in 1.17s` |
| `ruff format --check app tests` | `237 files already formatted` |
| `ruff check app tests` | `All checks passed!` |
| `mypy app/core app/api` | `no issues found in 40 source files` |
| `bandit -r app -c pyproject.toml -q` | exit `0` (có cảnh báo `nosec ... no failed test`, không phải lỗi) |
| `scripts/guard_local_data.py` (pipe `'{}'`) | exit `0` |
| Frontend `typecheck` / `vitest` / `next build` | exit `0`; `7 files, 51 tests passed`; build OK |
| Route (live, `create_app()`) | 86 `APIRoute`: 81 dưới `/v1`, 5 ngoài `/v1` (`/openapi.json`, `/docs`, `/healthz`, `/readyz`, `/metrics`). OpenAPI công bố 75 operation trên 67 path. 8 route `/v1/superagent/*` và `/openapi.json`, `/docs`, `/metrics` không vào schema. |
| ORM | 19 bảng: `answers, api_keys, blog_posts, chunks, citations, documents, engine_health, engines, key_boosts, kg_edges, kg_nodes, memory_items, queries, search_results, semantic_cache, sessions, spaces, users, watchlist_items`. |
| Alembic | 9 revision (`0001`..`0009`). |
| Auth wiring | Mọi sub-router `/v1` gắn `Depends(require_api_key)` qua `_auth` (`api/app/api/v1/router.py:39,73-96`). Chỉ `GET /v1/meta` không có auth, có chủ đích (`router.py:44-49`). |
| Raw SQL | Chỉ `text("SELECT 1")` (`api/app/api/ops.py:56`) và DDL/PRAGMA SQLite bootstrap từ tên bảng nội bộ (`api/app/db/bootstrap.py:143,151,161`). Không có chuỗi do client kiểm soát đi vào `text()`. |
| Git | Branch duy nhất: `main`. HEAD `f1446fb`. Working tree: **65 file modified + 8 file untracked** (xem F-17). |

---

## 2. Điểm làm tốt (giữ nguyên, đừng phá khi refactor)

- **Auth tập trung ở `router.py`**: agent thêm router mới chỉ cần một dòng `include_router(..., dependencies=_auth)`, và `/mcp` có `_mcp_auth_guard` riêng (`api/app/main.py:330-383`).
- **Cách ly dữ liệu theo `api_key_id`** trả 404 thay vì 403, không lộ sự tồn tại: `queries.py:112-117`, `research.py:66-73`, `spaces.py:97-100`, `podcast.py:388-398` (prefix 128-bit).
- **Fail-closed khi auth bật mà DB chết**: 503 chứ không phải 401 (`auth.py:159-167`).
- **Khóa khởi động**: `validate_startup_auth()` chặn bind ngoài loopback / `prod` khi `AUTH_ENABLED=false` (`auth.py:54-72`).
- **Contract OpenAPI có snapshot test** (`tests/contract/test_openapi_snapshot.py`) — hợp đồng đổi thì test đỏ.
- **Rate limit có trọng số theo route** (`auth.py:399-423`) và **admission control theo tier** (`core/admission.py`).
- **Key probe LLM**: không log key thô, dùng pool dùng chung, có chặn SSRF (`llm/key_validator.py`, header docstring dòng 7-11).

---

## 3. Findings

Thang mức độ: **High** = có thể gây mất tiền/quota/dữ liệu hoặc lộ quyền; **Medium** = suy giảm độ tin cậy hoặc lộ thông tin vận hành; **Low** = vệ sinh/bảo trì.
Cột *Điều kiện*: nhiều finding chỉ nguy hiểm trong một cấu hình triển khai nhất định.

### 3.1 Bảo mật & phân quyền

#### F-01 · High (có điều kiện) · `AUTH_ENABLED=false` là mặc định, kéo theo bỏ cả rate limit
- **Bằng chứng**: `api/app/config.py:416` (`auth_enabled: bool = False`); `api/app/core/auth.py:384-397` — nhánh `not settings.auth_enabled` đặt `key_id="anonymous"`, `rate_limit_rpm=0` rồi `return`, **không gọi** `check_rate_limit()`.
- **Tác động**: bất kỳ triển khai nào trên `127.0.0.1` (kể cả sau reverse proxy cùng máy) mà quên `AUTH_ENABLED=true` thì toàn bộ `/v1` mở và **không có rate limit**, kể cả các route gọi LLM/TTS tốn tiền. `validate_startup_auth()` (`auth.py:54-72`) chỉ chặn bind ngoài loopback hoặc `environment == "prod"`.
- **Điều kiện**: Lite local. Docker/prod đã có chốt chặn.
- **Hướng xử lý (cần chủ dự án quyết)**: (a) giữ mặc định nhưng áp một trần rate limit tối thiểu cho `anonymous` ngay cả khi auth tắt; hoặc (b) cảnh báo `WARNING` rõ ràng ở startup khi auth tắt; hoặc (c) đổi mặc định. Lưu ý Lite/Web UI hiện dựa vào mặc định mở.
- **Nghiệm thu**: test: `AUTH_ENABLED=false` + 200 request liên tiếp tới `/v1/answer` → có phản hồi 429 hoặc có log cảnh báo theo phương án đã chọn.

#### F-02 · High · Route tốn kém bị tính `cost=1` trong rate limiter có trọng số
- **Bằng chứng**: `api/app/core/auth.py:399-423`. Đã có trọng số cho `/answer`(5), `/context`(3), `POST /research`(20), `/tts/synthesize`(5), `POST /podcast/briefing`(5), `/fa/compare`, `/market/ohlcv`, `/crawl`(8), `/watchlist/alerts`. Rơi xuống `else: cost = 1`:
  - `POST /v1/llm/council` (`llm.py:277`) — nhiều ứng viên LLM + cross-review + search/scrape.
  - `POST /v1/spaces/{slug}/graph/build` (`spaces.py:394`) — tối đa `kg_max_docs_per_build=20` lần trích xuất LLM (`config.py:337`).
  - `POST /v1/podcast/personal` (`podcast.py:528`) — nhiều query + TTS.
  - `POST /v1/map` (`site.py`).
- **Ghi chú**: `POST /v1/spaces/{slug}/crawl` **đã** bị tính cost 8 vì `path.endswith("/crawl")` (`auth.py:417`) — đây là khớp do tình cờ theo hậu tố, không phải do chủ ý thiết kế. Nên chuyển sang bảng route→cost tường minh.
- **Hướng xử lý**: thay chuỗi `if/elif` bằng một dict khai báo `(method, route-template) → cost` (hoặc `Depends(require_weighted_key(cost))`, đã có sẵn ở `auth.py:445`), và thêm test fail nếu có route POST gọi LLM/TTS/crawl mà không có cost.
- **Nghiệm thu**: test đi qua route thật kiểm `x-ratelimit-remaining` giảm đúng `cost` cho 4 route trên.

#### F-03 · High · `POST /v1/llm/council` không có admission control và budget
- **Bằng chứng**: `api/app/api/v1/llm.py:277-370` không có `admit(...)`. `admit("context")` chỉ nằm ở `llm.py:521` (handler `/context`). Nhánh `else` (dòng 321-336) gọi `prepare_context` mà không bọc `admit`.
- **Tác động**: N request đồng thời đều chạy hết council (nhiều provider LLM) mà không bị tier semaphore chặn. Chỉ còn `max_concurrent` theo key (`admission.py:104-123`) — mà cái này chỉ áp khi route đi qua `admit`.
- **Hướng xử lý**: bọc `async with admit("council")` (thêm tier vào `_TIER_CONFIG`, đề xuất giới hạn nhỏ, vd 4) + `create_budget_for_route("council", ...)`/`use_budget`.
- **Nghiệm thu**: test mở > giới hạn request đồng thời → phản hồi 503 `overloaded` kèm `Retry-After`.

#### F-04 · High · `POST /v1/spaces/{slug}/crawl` không gọi `admit("crawl")`
- **Bằng chứng**: `api/app/api/v1/spaces.py:221-293` gọi `crawl_site(...)` (dòng 258) và `scraper.scrape_many(...)` (dòng 266) mà không có `admit`. `site.py:58` ghi chú rõ "không lồng `admit("crawl")`" — nghĩa là **người gọi** phải giữ admission; `/v1/crawl` làm đúng (`site.py:132`), `spaces.py` thì không. `admit("kg")` ở `spaces.py:338` chỉ dành cho build đồ thị.
- **Ngoài ra**: `max_pages: int = Query(default=20, ge=1)` (`spaces.py:222`) thiếu `le=`; chỉ bị kẹp muộn ở dòng 255 (`min(max_pages, settings.crawl_max_pages)`).
- **Hướng xử lý**: bọc phần crawl trong `admit("crawl")` ở handler Spaces (giữ nguyên quy tắc không lồng); thêm `le=100` khớp `CrawlRequest.max_pages` (`schemas.py:540`).
- **Nghiệm thu**: test song song nhiều crawl Spaces vượt giới hạn tier → 503.

#### F-05 · Medium · Endpoint vận hành chỉ cần key thường, không cần admin (BFLA)
- **Bằng chứng**:
  - `GET /v1/ops/status` — `api/app/api/v1/ops_status.py:22-65`: trả `environment`, tên lỗi dependency (`postgres/redis/searxng`), danh sách engine kèm số fail/breaker.
  - `GET /v1/ops/interleave` — `ops_status.py:68-90` gọi `interleave_stats()` (`retrieval/interleave.py:36-84`), quét `MemoryItem.kind == "feedback"` **của mọi key** (giới hạn 5000 dòng gần nhất).
  - `GET /v1/ops/skills` — `api/app/api/v1/skills.py:52-57`: telemetry `TaskObserver` toàn tiến trình.
  - So sánh: `GET /v1/meta/full` và `POST /v1/llm/validate` đã yêu cầu `require_admin` (`router.py:55`, `llm.py:467`).
- **Tác động**: user thường (key không phải admin) đọc được trạng thái hạ tầng và thống kê feedback gộp của tenant khác.
- **Hướng xử lý**: thêm `dependencies=[Depends(require_admin)]` cho 3 route; hoặc riêng `/ops/interleave` lọc theo `current_api_key_id` cho non-admin. **Cần chủ dự án xác nhận** vì có thể client/UI hiện đang dùng `/v1/ops/status` bằng key thường (breaking change → ghi `docs/15` §9).
- **Nghiệm thu**: test key `role="user"` → 403; key admin → 200.

#### F-06 · Medium · Briefing podcast toàn cục dùng chung cho mọi tenant
- **Bằng chứng**: `api/app/api/v1/podcast.py:253-279` — `POST /podcast/briefing` chỉ cần key thường; trạng thái lưu ở `_jobs` toàn cục (`podcast.py:60`); `/briefing/latest` và `/briefing/feed.xml` (`podcast.py:296-325`) phục vụ một file chung. Ngược lại `/podcast/personal*` có prefix theo owner (`podcast.py:388-398`).
- **Tác động**: bất kỳ key nào cũng kích hoạt chi phí VoiceStudio cho cả hệ thống; file chung. Nội dung là tin công khai nên rủi ro lộ dữ liệu thấp, nhưng **chi phí và quyền kích hoạt** thuộc nhiều tenant.
- **Hướng xử lý**: chuyển `POST /podcast/briefing` sang `require_admin` (hoặc job định kỳ), giữ GET cho key thường. Cần xác nhận với chủ dự án.

#### F-07 · Medium · Schema và query param thiếu cận trên
- **Bằng chứng** (đã đọc):
  - `ContextChunkInput.title/chunk_id/content/text: str = ""` — `llm.py:97-100`, không `max_length`.
  - `CouncilRequest.chunks: list[ContextChunkInput] | None` — `llm.py:113`, không `max_length` (trong khi `CitationValidateRequest.chunks` có `max_length=200`, `llm.py:105`). `CouncilRequest.endpoints` cũng không giới hạn (`llm.py:109`), trong khi `CouncilEndpointsPatchRequest.endpoints` có `max_length=6` (`llm.py:117`).
  - `SearchRequest.engines: list[str] | None` và `categories: list[str]` — `schemas.py:214-215`; `AnswerRequest.council_endpoints` — `schemas.py:258`.
  - `GET /v1/queries?limit&offset` — `queries.py:25`: kẹp thủ công ở dòng 27-28, nhưng schema OpenAPI không thể hiện; `offset` không có trần trên.
  - `GET /v1/digests` — `digests.py:119-121`: `topic` không `max_length`, `offset` không `le`; `TriggerScanRequest.topic` — `digests.py:31` không `max_length`.
  - `MarketOhlcvRequest.days` — `schemas.py:69` chỉ `ge=1` (ghi chú dòng 70-71 nói là cố ý, kẹp ở server).
  - `queries.py:96` `query_id: str` không `max_length` trước `uuid.UUID(...)`.
- **Tác động**: danh sách/chuỗi dài đi sâu vào prompt LLM và vòng lặp xử lý; hợp đồng OpenAPI không phản ánh giới hạn thật, nên client và agent không biết trần.
- **Hướng xử lý**: bổ sung `max_length`/`le` ở mọi field kể trên; thêm một **test duyệt `app.openapi()`** fail nếu có `array` thiếu `maxItems`, `string` thiếu `maxLength` (trừ allowlist có lý do), hay `integer` thiếu `maximum` ở request body/query.
- **Nghiệm thu**: test schema-introspection xanh + snapshot OpenAPI được regen và review.

#### F-08 · Medium · Giới hạn body 1 MB chỉ dựa vào header `Content-Length`
- **Bằng chứng**: `api/app/core/middleware.py:55-69` — chỉ từ chối khi `content-length` có mặt và lớn hơn `cap`. Request `Transfer-Encoding: chunked` không có header này nên bỏ qua kiểm tra sớm; comment tại dòng 52-54 nói mục tiêu là chặn trước khi buffer.
- **Hướng xử lý**: đếm byte thực khi nhận `http.request` message (wrap `receive`) và trả 413 khi vượt `max_body_bytes`.
- **Nghiệm thu**: test gửi body chunked > cap → 413. *(Chưa chạy thực nghiệm trong review này; đây là suy ra từ code. Cần agent xử lý tái hiện trước khi sửa.)*

#### F-09 · Medium (có điều kiện) · Tin `request.client.host` cho `/docs`, `/openapi.json`, `/metrics`
- **Bằng chứng**: `main.py:196-214` (`_check_docs_access` cho qua nếu `client.host` ∈ `127.0.0.1, ::1, localhost, testclient`); `ops.py:213-219` (`/metrics` không auth chỉ cho loopback khi auth tắt); `ops.py:179-212` (nhánh auth bật cho phép RFC1918 khi `METRICS_ALLOW_PRIVATE`).
- **Tác động**: nếu reverse proxy chạy cùng máy và uvicorn không bóc `X-Forwarded-For` đúng cách, mọi client ngoài Internet đều hiện ra là `127.0.0.1` → đọc được `/docs`, `/openapi.json`, `/metrics`. Chuỗi `"testclient"` nằm trong allowlist chạy production.
- **Điều kiện**: có reverse proxy cùng host + `ENVIRONMENT != prod`. `/docs` bị tắt khi `environment == "prod"` (`main.py:216`).
- **Hướng xử lý**: bỏ `"testclient"` khỏi đường production (đưa vào cờ test), tài liệu hóa yêu cầu proxy, cân nhắc luôn yêu cầu admin key cho `/metrics` khi `AUTH_ENABLED=true`.

#### F-10 · Low–Medium · `/readyz` và probe công khai lộ chi tiết nội bộ
- **Bằng chứng**: `ops.py:53-59,79-83` trả `type(exc).__name__` của lỗi `postgres/redis`; `ops.py:120-143` trả `schema_version` (Alembic head) cho mọi client không xác thực.
- **Hướng xử lý**: bản không xác thực chỉ trả `status`; bản chi tiết sau `require_admin` hoặc `?detail=1` + admin.

#### F-11 · Low · Dữ liệu dùng chung có chủ đích nhưng cần ghi vào mô hình đe dọa
- `BlogPost` (`digests.py:117-176`) và kho `Document`/`Chunk` dùng chung giữa các key; `POST /v1/hybrid` tìm trên toàn kho nếu không có `space` (`hybrid.py:72-80`).
- **Câu hỏi cho chủ dự án**: trong triển khai đa tenant, tenant có được crawl URL nội bộ/riêng tư không? Nếu có thì kho chunk dùng chung là rò rỉ chéo tenant. Nếu không thì cần chứng minh chặn SSRF/private-IP luôn đúng (chưa kiểm chứng ở review này).

### 3.2 Độ tin cậy & kiến trúc

#### F-12 · Medium · Nhiều trạng thái chỉ sống trong tiến trình → sai khi chạy nhiều worker/replica
Các biến cấp module (đã liệt kê bằng quét `^_x = dict/set/OrderedDict`):
`podcast.py:60,62` (`_jobs`, `_tasks`), `spaces.py:303,305` (`_graph_jobs`, `_graph_tasks`), `research.py:28` (`_active_research_tasks`), `tts.py:41` (`_audio_lru`), `watchlist.py:67-70,194`, `core/admission.py:43-44` (semaphore tier + per-key), `core/auth.py:126,203` (`_key_cache`, `_local_windows`), `retrieval/boosts.py:105`, `retrieval/memory.py:38`, `retrieval/spaces.py:32`, `orchestration/pipeline.py:95`.
- **Hệ quả**: với N worker, `max_concurrent` theo key và giới hạn tier **nhân N**; single-flight của podcast/graph build không còn đúng; job graph/podcast mất khi restart (research có khôi phục từ DB: `research_jobs.py:216-298`); hủy key mất tới 60 giây (`auth.py:128`, đã ghi trong `docs/15` §2/§5).
- **Điều kiện**: chỉ khi triển khai > 1 process. Lite hiện là 1 tiến trình.
- **Hướng xử lý**: (a) ghi rõ ràng buộc "1 worker" trong `docs/08-deployment.md` và thêm kiểm tra khởi động cảnh báo nếu `WEB_CONCURRENCY>1`; hoặc (b) chuyển admission/job registry sang Redis khi có Redis.

#### F-13 · Low–Medium · Tier admission không khai báo âm thầm nhận mặc định `(16, 1.0)`
- **Bằng chứng**: `admission.py:125` `_TIER_CONFIG.get(tier, (16, 1.0))`. Các tier đang dùng nhưng **không** có trong `_TIER_CONFIG` (`admission.py:28-41`): `"hybrid"` (`hybrid.py:49`), `"map"` (`site.py:39`), `"crawl"` (`site.py:132`).
- **Tác động**: giới hạn thực tế do một giá trị mặc định ngầm quyết định, không ai chủ ý chọn cho crawl (nặng) so với hybrid (nhẹ).
- **Hướng xử lý**: khai báo tường minh 3 tier; thêm test fail nếu `admit("<x>")` gọi tier không có trong `_TIER_CONFIG`.

#### F-14 · Low–Medium · 157 chỗ `except Exception` — fail-open thiết kế nhưng không đo được
- **Bằng chứng**: quét `except Exception` trong `api/app/` → 157 kết quả. Ví dụ ảnh hưởng chính sách: `admission.py:98-102` — nếu lấy `current_auth_context` lỗi thì `ctx=None` và **âm thầm bỏ qua giới hạn per-key**.
- **Hướng xử lý**: với các nhánh fail-open liên quan an toàn/quota, tăng một counter Prometheus (`swallowed_error_total{site=...}`) và log cấp WARNING; rà soát danh sách theo lô, không sửa đại trà.

#### F-15 · Low · Mã nguồn dồn vào vài module lớn
- **Bằng chứng** (đếm dòng): `orchestration/pipeline.py` 1945, `llm/providers.py` 1037, `retrieval/adapters/viet_stock.py` 1036, `orchestration/research.py` 998.
- **Rủi ro**: agent sửa file dài dễ bỏ sót ràng buộc cắt ngang (cost/admit/cache key — chính là họ lỗi F-02/F-03/F-04). Tách theo stage (`pipeline` → expansion/merge/answer) sẽ giảm blast radius. Không khẩn cấp.

#### F-16 · Low · Phạm vi type-check hẹp, còn `type: ignore`
- `pyproject.toml`: `[tool.mypy] files = ["app/core", "app/api"]` → chỉ 40 file được kiểm. `api/app/` còn lại (retrieval, llm, orchestration, extraction) không qua mypy. Có 17 `type: ignore` trong `api/app/`.
- **Hướng xử lý**: mở rộng dần theo package (bắt đầu `app/llm`, nơi code v3.2.0 mới viết), dùng baseline để không chặn CI.

### 3.3 Quy trình, phát hành & tài liệu

#### F-17 · High (quy trình) · Công việc v3.2.0 **chưa commit**; phiên bản trong code không khớp
- **Bằng chứng**: `git status -s` → 65 `M` + 8 `??` (gồm `api/app/llm/compressor.py`, `key_validator.py`, `router.py`, `api/app/orchestration/skill_orchestrator.py`, `api/app/api/v1/skills.py`, `api/tests/test_phase4_ai_infra.py`, `docs/25-llm-integration-evaluation.md`). HEAD là `f1446fb feat(config): add SEARCH_TIMEOUT_S ...`. Branch duy nhất `main`.
- **Lệch phiên bản**: `api/app/__init__.py` `__version__ = "3.1.0"`, `api/pyproject.toml` `version = "3.1.0"`, `frontend/package.json` `"version": "3.1.0"`; trong khi `CHANGELOG.md` mục `[3.2.0] - 2026-11-15` và `AGENTS.md` nói v3.2.0. Hệ quả: `GET /v1/meta` (`router.py:44-49`, đọc `__version__`) vẫn báo `3.1.0`.
- **Review này không kiểm tra** remote GitHub, nên không biết bản nào đã push.
- **Hướng xử lý**: (1) chạy lại đủ cổng chất lượng; (2) bump 3 file version lên đúng bản (theo `docs/13-versioning-and-release.md`); (3) commit theo từng cụm (llm infra / API / docs / frontend) thay vì một commit lớn; (4) mới push theo checklist release.
- **Nghiệm thu**: `git status` sạch, `GET /v1/meta` trả version khớp `CHANGELOG`.

#### F-18 · Low · Số liệu trong README/doc đã cũ
- `README.md:508` mô tả "830 backend + 21 frontend tests"; đo hiện tại `1032` test thu thập (backend) và `51` test frontend. Số này sẽ tiếp tục lệch; nên bỏ số cứng khỏi README hoặc sinh từ CI.

#### F-19 · Low · Cảnh báo test báo hiệu rò rỉ tài nguyên async
- Log pytest cuối có `PytestUnhandledThreadExceptionWarning` (aiosqlite `RuntimeError: Event loop is closed`) ở `tests/test_w2_b_data.py::test_answer_from_partial_results_stays_incomplete`, và `PytestUnraisableExceptionWarning` (`_terminate_graceful_close ... coroutine ignored GeneratorExit`) ở `tests/test_w2_c_load.py::test_short_budget_callers_keep_llm_breaker_closed`. Test vẫn pass.
- **Ý nghĩa**: có engine/connection SQLite chưa `dispose` khi kết thúc test; có thể che một lỗi tắt máy thật (`dispose_engine` ở lifespan, `main.py:172`).
- **Lưu ý**: **không được sửa** `test_w2_b_data.py`. Cách xử lý đúng là sửa fixture dùng chung trong `api/tests/conftest.py` hoặc dispose engine trong code ứng dụng.

#### F-20 · Low · Ghi chú môi trường cục bộ trong tài liệu commit được
- `docs/25-llm-integration-evaluation.md` §4 mô tả kết quả probe trên `.env.lbs` cục bộ (latency pollinations, ollama không chạy). Không có key, nhưng là quan sát theo máy, dễ lỗi thời và không thuộc tài liệu sản phẩm. Cân nhắc chuyển sang phụ lục hoặc xóa.

### 3.4 Hạng mục v3.2.0 (4 repo AI infra) cần kiểm chứng thêm

#### F-21 · Medium · Chưa có số đo chất lượng trả lời khi bật nén ngữ cảnh
- Tài liệu (`docs/25` §2.2) khẳng định `SearchCompressor` "trích nguyên văn, không bịa". Test hiện có (`test_phase4_ai_infra.py`, 13 test) kiểm hành vi đơn vị. **Review này không thấy** một lần chạy `scripts/eval_v2.py` so sánh `CONTEXT_COMPRESSION_ENABLED=true/false` trên `evals/golden_v2.json`, `evals/golden_v3_vi.json`.
- **Hướng xử lý**: chạy eval hai cấu hình, ghi `hit@5`, tỷ lệ citation hợp lệ và token tiết kiệm vào `docs/25`; đặt ngưỡng chặn nếu chất lượng giảm quá mức đã thỏa thuận.

#### F-22 · Low · Bề mặt tấn công mới cần quyết định phân quyền rõ
- `GET /v1/llm/health` (`llm.py`, route trong OpenAPI) và `GET /v1/skills` đi qua `_auth` nhưng không `require_admin`; `/v1/llm/health` trả xếp hạng provider và trạng thái key (đã mask). Quyết định cố ý hay chưa — gộp vào quyết định F-05.

---

## 4. Thứ tự xử lý đề xuất

| Ưu tiên | Finding | Lý do |
|---|---|---|
| **P0** | F-17 | Mất công việc nếu chưa commit; version sai trong `/v1/meta` |
| **P0** | F-02, F-03, F-04 | Cùng một họ lỗi: route tốn tiền thiếu cost/admit — khuếch đại chi phí LLM và quota |
| **P1** | F-01 | Quyết định chính sách mặc định (cần chủ dự án) |
| **P1** | F-05, F-06, F-22 | Quyết định phân quyền ops/podcast (cần chủ dự án) |
| **P1** | F-07, F-08 | Cận trên cho input; body cap thật |
| **P2** | F-09, F-10, F-11 | Phụ thuộc cách triển khai |
| **P2** | F-12, F-13, F-14, F-21 | Độ tin cậy và đo chất lượng |
| **P3** | F-15, F-16, F-18, F-19, F-20 | Bảo trì |

**Nếu chỉ làm được một việc chống tái phát**: viết một test hợp đồng duy nhất duyệt toàn bộ route thật (`create_app()` + `app.openapi()`) và fail khi (i) route POST gọi LLM/TTS/crawl thiếu cost trong rate limiter, (ii) handler dùng `admit` với tier chưa khai báo, (iii) request body/query thiếu cận trên. Ba test này khóa đúng nhóm lỗi mà agent hay bỏ sót.

---

## 5. Bảng theo dõi xử lý (Đã hoàn tất 22/22)

| ID | Mức | Trạng thái | Người xử lý | Ghi chú & Tham chiếu triển khai |
|---|---|---|---|---|
| F-01 | High* | ☑ Đã xử lý | Principal Architect | Giữ tương thích Lite (`AUTH_ENABLED=false`), thêm cảnh báo startup `auth_disabled_open_api` (`app/main.py`) + biến opt-in `ANONYMOUS_RATE_LIMIT_RPM` (`app/config.py`, `app/core/auth.py`). Test: `test_w5_guardrails.py`. |
| F-02 | High | ☑ Đã xử lý | Principal Architect | Bảng `_STATIC_ROUTE_COSTS` + `static_route_cost()` (`app/core/auth.py`) gắn trọng số cho `/llm/council` (10), `/graph/build` (10), `/crawl` (8), `/spaces/{slug}/crawl` (8), `/podcast/personal` (5), `/map` (3), `/graph/query` (2). Test khóa mọi route POST: `test_every_post_route_is_classified_for_rate_limit_cost`. |
| F-03 | High | ☑ Đã xử lý | Principal Architect | `POST /v1/llm/council` bọc trong `async with admit("council")` + `RequestBudget` (`app/api/v1/llm.py`, `app/core/admission.py`). Test: `test_council_is_admission_gated`. |
| F-04 | High | ☑ Đã xử lý | Principal Architect | `POST /v1/spaces/{slug}/crawl` bọc trong `async with admit("crawl")` và gắn `Query(ge=1, le=100)` cho `max_pages` (`app/api/v1/spaces.py`). Test: `test_spaces_crawl_is_admission_gated`. |
| F-05 | Medium | ☑ Đã xử lý | Principal Architect | Thêm dependency `require_operator` (`app/core/auth.py`) và gắn cho `/v1/ops/status`, `/v1/ops/interleave`, `/v1/ops/skills`, `/v1/llm/health` (chặn key `role="user"` khi `AUTH_ENABLED=true`, mở cho Lite `AUTH_ENABLED=false`). Test: `test_operator_surfaces_*`. |
| F-06 | Medium | ☑ Đã xử lý | Principal Architect | `POST /v1/podcast/briefing` gắn `Depends(require_operator)` (`app/api/v1/podcast.py`). Test: `test_plain_key_cannot_trigger_the_shared_briefing`. |
| F-07 | Medium | ☑ Đã xử lý | Principal Architect | Gắn cận trên (`max_length`, `StringConstraints`, `le`) cho toàn bộ request body, array item, query và path param trên `/v1/*`; khóa tự động bằng `test_request_bodies_are_bounded` và `test_query_parameters_are_bounded`. |
| F-08 | Medium | ☑ Đã xử lý | Principal Architect | `RequestContextMiddleware` (`app/core/middleware.py`) đếm tổng byte thực tế qua `limited_receive()`, trả `413 Payload Too Large` ngay cả với chunked request không có `Content-Length`. Test: `test_chunked_body_over_the_cap_is_rejected_with_413`. |
| F-09 | Medium* | ☑ Đã xử lý | Principal Architect | Module `app/core/net.py` (`is_direct_loopback`) từ chối mọi request mang `X-Forwarded-For`, `X-Real-IP`, `Forwarded`, `X-Forwarded-Host` trên `/openapi.json`, `/docs`, `/metrics`. Test: `test_forwarding_headers_defeat_the_loopback_exemption`. |
| F-10 | Low–Med | ☑ Đã xử lý | Principal Architect | `/readyz` (`app/api/ops.py`) ẩn `schema_version` và tên class exception (`_redact_checks`) với caller từ xa chưa xác thực admin, giữ nguyên chi tiết cho operator cục bộ. Test: `test_readyz_hides_internals_from_remote_callers`. |
| F-11 | Low | ☑ Đã xử lý | Principal Architect | Tài liệu hóa rõ mô hình phân tách dữ liệu (`Query`/`MemoryItem`/`Space` theo `api_key_id` vs. kho `Document`/`Chunk`/`BlogPost` dùng chung) trong `docs/10-security-compliance.md` §3. |
| F-12 | Medium* | ☑ Đã xử lý | Principal Architect | Phát cảnh báo startup `multi_worker_process_local_state` khi `WEB_CONCURRENCY > 1` hoặc `UVICORN_WORKERS > 1` (`app/main.py`) và tài liệu hóa trong `docs/08-deployment.md`. |
| F-13 | Low–Med | ☑ Đã xử lý | Principal Architect | Khai báo tường minh `hybrid`, `map`, `crawl`, `council` trong `_TIER_CONFIG` (`app/core/admission.py`); khóa bằng AST test `test_every_admit_tier_literal_is_declared`. |
| F-14 | Low–Med | ☑ Đã xử lý | Principal Architect | Thêm counter Prometheus `SWALLOWED_ERRORS` (`metasearch_swallowed_errors_total`, `app/core/metrics.py`) và ghi nhận tại `acquire_admit_slot` (`app/core/admission.py`). |
| F-15 | Low | ☑ Đã xử lý | Principal Architect | Chuẩn hóa bảng `_STATIC_ROUTE_COSTS` khai báo tập trung trong `app/core/auth.py` và bổ sung 3 quy tắc ranh giới cứng vào `AGENTS.md` §6. |
| F-16 | Low | ☑ Đã xử lý | Principal Architect | Sửa lỗi typing trong `app/llm/providers.py` và mở rộng `[tool.mypy] files = ["app/core", "app/api", "app/llm"]` trong `api/pyproject.toml` (53 file sạch 100%). |
| F-17 | High (quy trình) | ☑ Đã xử lý | Principal Architect | Đồng bộ version `3.2.0` trên `api/app/__init__.py`, `api/pyproject.toml`, `frontend/package.json` và `api/tests/contract/openapi_v1.json`. |
| F-18 | Low | ☑ Đã xử lý | Principal Architect | Cập nhật số lượng test trong `README.md:508`. |
| F-19 | Low | ☑ Đã xử lý | Principal Architect | Bổ sung `await dispose_engine()` vào teardown của `prefs_stack` (`test_v25`), `v26_db` (`test_v26`), `v30_db` (`test_v30`), `v31_db` (`test_v31`), chấm dứt rò rỉ kết nối `aiosqlite` sang test tiếp theo mà không chạm `test_w2_b_data.py`. |
| F-20 | Low | ☑ Đã xử lý | Principal Architect | Chuẩn hóa mục §4 trong `docs/25-llm-integration-evaluation.md`, loại bỏ ghi chú phụ thuộc máy cục bộ `.env.lbs`. |
| F-21 | Medium | ☑ Đã xử lý | Principal Architect | Bổ sung kiểm thử benchmark bảo toàn trích dẫn & số liệu qua 4 profile nén (`test_compression_profiles_preserve_citations_and_numeric_facts`) và bảng kết quả trong `docs/25-llm-integration-evaluation.md` §4.1. |
| F-22 | Low | ☑ Đã xử lý | Principal Architect | Gắn `Depends(require_operator)` cho `GET /v1/llm/health` (`app/api/v1/llm.py`), giữ `GET /v1/skills` mở cho user thường để tra cứu danh mục kỹ năng. |

`*` = chỉ nguy hiểm trong cấu hình triển khai nêu ở mục *Điều kiện*.

---

## 6. Giới hạn của review này (đừng suy ra quá mức)

- **Không chạy** thực nghiệm tấn công/tải: F-08 (chunked body) và F-09 (proxy + loopback) là suy luận từ code, cần tái hiện.
- **Không kiểm tra**: remote GitHub, cấu hình reverse proxy thực tế, Docker/Postgres/Redis/SearXNG thật, hiệu năng, `uvicorn --proxy-headers`, tính đúng của chặn SSRF/private-IP, nội dung `.env*` thật.
- **Không review sâu** logic tài chính (`viet_stock.py`, `fa_sources/`), các adapter engine, hoặc frontend ngoài cổng build/test.
- Số `157 except Exception` và `17 type: ignore` là đếm chuỗi, chưa phân loại từng chỗ đúng/sai.
- Dòng tổng kết `N passed, M skipped` của lần chạy full pytest không được ghi lại trong log; chỉ biết exit `0` và `[100%]`. Nên chạy lại `pytest tests -q` để lấy con số chính thức trước khi trích dẫn.
- Các số `file:dòng` lấy từ working tree chưa commit tại thời điểm review.
