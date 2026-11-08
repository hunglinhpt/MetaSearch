# 15 — Hướng dẫn tích hợp cho hệ thống ngoài

Tài liệu này dành cho đội tích hợp một hệ thống bên ngoài với MetaSearch qua REST `/v1`. MetaSearch chỉ cung cấp công cụ tìm kiếm và ngữ cảnh dùng chung; nghiệp vụ của hệ thống gọi tới nằm ở phía hệ thống đó, không nằm trong repo này. Hợp đồng được khóa bằng snapshot `api/tests/contract/openapi_v1.json`; đổi hợp đồng phải cập nhật snapshot và ghi vào mục 9 dưới đây.

> **Trạng thái:** chương trình readiness (`docs/14-readiness/`) chưa xong. Đọc `docs/14-readiness/README.md` trước khi nối hệ thống thật.

## 1. Quy trình tiếp nhận

1. Thống nhất endpoint, tải dự kiến (request đồng thời, request/phút) và SLO ở mục 6.
2. Cấp một key riêng cho hệ thống đó (mục 2). Không dùng chung key giữa các hệ thống.
3. Chạy kiểm tra hợp đồng từ máy của hệ thống ngoài (mục 7). Lưu log làm bằng chứng.
4. Bật tích hợp sau một cờ ở phía hệ thống ngoài, để tắt được ngay (mục 5).

## 2. Cấp key

Bật xác thực bằng `AUTH_ENABLED=true`. Tạo key bằng CLI, trên máy có quyền vào DB:

```bash
metasearch keys create my-system --rate-limit 600 --max-concurrent 16
```

- Key in ra **một lần**. Gửi kèm header `Authorization: Bearer <key>`.
- `--rate-limit`: request mỗi phút. `/v1/answer` tính 5 đơn vị.
- `--max-concurrent`: số request **đồng thời** của key đó, áp dụng cho mọi route (search, context, answer, scrape), không chỉ research. **Mặc định là 5.** Request thứ 6 nhận 429 kèm `Retry-After`. Key khác không bị ảnh hưởng.
- Giới hạn theo key được nạp lần đầu key được dùng. Đổi `max_concurrent` trong DB thì phải restart để có hiệu lực.
- `metasearch keys list` liệt kê key theo prefix, không bao giờ in key.

## 3. Chọn endpoint

| Nhu cầu | Endpoint | Ghi chú |
|---|---|---|
| Danh sách link có xếp hạng, không cần nội dung trang | `POST /v1/search` | Nhanh nhất. Có cache. |
| Ngữ cảnh đã scrape, rerank cho **LLM của bạn** | `POST /v1/context` | Mặc định trả `chunks` và `sources`. |
| Câu trả lời có trích dẫn, dùng LLM của MetaSearch | `POST /v1/answer` | JSON hoặc SSE (`"stream": true`). |
| Hậu kiểm `[n]` trong câu trả lời LLM của bạn | `POST /v1/citations/validate` | Xem `marker_valid` ở mục 3.4. |
| Báo cáo nhiều vòng (30-180 s) | `POST /v1/research`, rồi `GET /v1/research/{id}` | Chạy nền, chỉ chủ job đọc được. |

### 3.1 Tham số chung

- `timeout_ms` (1000-60000): ngân sách của request. Hết ngân sách mà chưa có kết quả thì nhận 504 `deadline_exceeded`.
- `include_domains`, `exclude_domains`: lọc theo host, `example.com` khớp cả subdomain. Lọc chạy sau tìm kiếm. Nếu bộ lọc loại hết kết quả, response có `results: []` và `complete: false`: **không có nghĩa là không tồn tại**.

### 3.2 Mốc dữ liệu

Mỗi kết quả search, mỗi `source` và mỗi `chunk` của context có:

| Trường | Ý nghĩa |
|---|---|
| `source_url` | Nơi kiểm chứng được dữ liệu. Với chunk lấy từ trang đã tải, là URL trang đó. |
| `data_kind` | `observed`, `derived`, `reference`, `computed`, hoặc `null` khi nguồn không nói. |
| `fetched_at` | Lúc MetaSearch tải trang (chỉ có ở chunk lấy từ trang). |
| `published_at` | Ngày nguồn công bố, nếu engine cung cấp. |
| `as_of` | Mốc thời gian của chính dữ liệu, ví dụ ngày của nến giá. |
| `age_s` | Tuổi của dữ liệu tính từ `as_of`, giây. |

**`null` nghĩa là không biết.** MetaSearch không tự chế mốc thời gian.

### 3.3 `/v1/context` và `include`

Mặc định `include: ["chunks", "sources"]`. Thêm phần cần thiết:

```json
{"query": "...", "k": 5, "include": ["chunks", "sources", "messages"]}
```

Giá trị hợp lệ: `chunks`, `sources`, `context_md`, `messages`, `system_prompt`, `domain_intelligence`. `context_md` và `messages` lặp lại nội dung chunk, nên payload mặc định nhỏ hơn khoảng một nửa. **Đây là thay đổi hành vi:** client cũ đọc `context_md` phải thêm nó vào `include`.

### 3.4 Citation

`/v1/citations/validate` trả `marker_valid` cho cả câu trả lời và cho từng citation. `marker_valid` chỉ nói marker `[n]` trỏ tới một chunk bạn đã gửi. Nó **không** kiểm tra chunk có ủng hộ câu đó hay không. `invalid_markers` liệt kê marker không trỏ tới chunk nào.

## 4. Xử lý lỗi

Mọi lỗi có dạng:

```json
{"error": {"code": "overloaded", "message": "...", "details": {}, "request_id": "..."}}
```

| HTTP | `code` | Ý nghĩa | Nên làm |
|---|---|---|---|
| 400 | `invalid_request` | Tham số sai | Sửa request, không thử lại |
| 401 | `unauthorized` | Thiếu hoặc sai key | Kiểm tra key |
| 404 | `not_found` | Không có tài nguyên, hoặc không phải của key này | Không thử lại |
| 429 | `rate_limited` | Vượt rate limit hoặc `max_concurrent` của key | Chờ `Retry-After` rồi thử lại |
| 502 | `upstream_timeout` | Upstream chậm | Thử lại có backoff |
| 503 | `overloaded` | Server đầy, từ chối trong ≤ 2 s | Chờ `Retry-After`, giảm đồng thời |
| 503 | `no_results` | Mọi engine lỗi | Thử lại sau; **không** coi là "không có kết quả" |
| 504 | `deadline_exceeded` | Hết `timeout_ms` hoặc hết trần thời gian của câu trả lời | Tăng `timeout_ms` hoặc bỏ qua |
| 500 | `internal` | Lỗi của MetaSearch | Báo lại kèm `request_id` |

**Rỗng thật và lỗi khác nhau:** `200` với `results: []` và `complete: true` là mọi engine đã trả lời mà không có gì. `complete: false` là thiếu dữ liệu: có engine lỗi, bị cắt, hoặc bộ lọc loại hết. SSE của `/v1/answer` báo lỗi bằng event `error` có cùng `code`; một stream quá trần `ANSWER_STREAM_MAX_S` (mặc định 120 s) kết thúc bằng event `error` mã `deadline_exceeded`.

## 5. Kill switch

Theo thứ tự, từ nhanh nhất:

1. **Phía hệ thống ngoài:** cờ tắt tích hợp, quay về hành vi không có MetaSearch. Bắt buộc có trước khi bật.
2. **Tắt key:** `UPDATE api_keys SET enabled = false WHERE key_prefix = 'msk_xxxxxxxx';` Có hiệu lực sau tối đa 60 s (cache xác thực).
3. **Tắt một engine gây lỗi:** `PATCH /v1/engines/{engine_id}` với `{"enabled": false}`, cần key admin, không restart.
4. **Dừng tiến trình MetaSearch.** Hệ thống ngoài phải chịu được việc này nhờ cờ ở bước 1.

## 6. SLO đề xuất

Theo `docs/14-readiness/04-quality-checklist.md` §4.1, Lite trên một tiến trình:

| Chỉ số | Mục tiêu |
|---|---|
| `/v1/search` cache miss, p95 trên mạng thật | ≤ 3 s |
| `/v1/search` cache hit, p95 | ≤ 50 ms |
| `/v1/context` k=5, p95 trên mạng thật | ≤ 8 s |
| Quá tải | Từ chối 503 trong ≤ 2 s, kèm `Retry-After` |
| Khả dụng trong giờ máy bật | ≥ 99% |

Đây là mục tiêu, chưa phải cam kết đã đo trên máy của hệ thống ngoài.

## 7. Kiểm tra hợp đồng

Từ bất kỳ máy nào gọi được API, chỉ cần `httpx`:

```bash
python api/scripts/contract_check.py --base-url http://HOST:PORT --key <key>
```

Script không có địa chỉ mặc định: luôn truyền `--base-url` của đúng instance cần kiểm. Exit code 0 khi mọi kiểm tra đạt.

## 8. Engine theo domain

Danh sách engine đang bật và sức khỏe của chúng: `GET /v1/engines`, `GET /v1/engines/health`. Domain nào ưu tiên engine nào: `POST /v1/domains/analyze` với `{"query": "..."}` trả `detected_domain` và `preferred_engines`. Bảng này thay đổi theo cấu hình, nên đọc từ API thay vì chép vào đây.

## 9. Thay đổi hợp đồng

| Ngày | Thay đổi |
|---|---|
| 2026-11-08 | `/v1/context` mặc định chỉ trả `chunks`, `sources`; thêm `include`. Thêm `include_domains`, `exclude_domains`. Thêm mốc dữ liệu cho kết quả, source, chunk. Thêm `marker_valid`. MCP: thêm tool `research_status`, bỏ `poll_hint`. |
