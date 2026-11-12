# 06 — API Specification (v1.0.0)

Base URL: `http://localhost:8000` · Auth: `Authorization: Bearer <api_key>` (khi `AUTH_ENABLED=true`; mặc định `false` cho local dev + Web UI).

## 1. POST /v1/search — Raw search

Request:
```json
{
  "query": "phân tích BCTC pháp y và kỹ thuật Fibonacci cổ phiếu FPT RSI 9",
  "lang": "vi",
  "k": 10,
  "engines": ["viet_stock", "yahoo_finance", "searxng"],
  "categories": ["general", "financial"],
  "freshness_days": 90,
  "include_snippets": true,
  "news": false
}
```
> Khi `categories` được để mặc định là `["general"]`, hệ thống tự động kích hoạt **Smart Category Routing** (`infer_categories_from_query`) để bổ sung các chuyên ngành phù hợp (`financial`, `fengshui`, `news`, `academic`, `developer`) nếu câu hỏi chứa tín hiệu chuyên biệt.

Response 200:
```json
{
  "query_id": "uuid",
  "results": [
    {
      "rank": 1,
      "url": "https://finance.vietstock.vn/FPT/tai-chinh.htm?tab=forensics",
      "title": "[FPT] Thẩm Định Pháp Y BCTC (Financial Forensics): Cấu Trúc Nợ, Phải Thu & Chất Lượng Lợi Nhuận",
      "snippet": "…",
      "score": 0.92,
      "consensus": 2,
      "found_by": ["viet_stock"],
      "domain": "finance.vietstock.vn",
      "published_at": "2026-11-01T00:00:00Z"
    }
  ],
  "engines_used": ["viet_stock", "yahoo_finance"],
  "engines_failed": [],
  "engines_status": [{"engine": "viet_stock", "status": "ok", "results": 4, "ms": 12}],
  "latency_ms": 420,
  "cached": false
}
```

## 2. POST /v1/answer — Quick answer (JSON hoặc SSE)

Request: như `/v1/search` + `"stream": true|false`, `"model": "gpt-4o-mini"` (optional).

Response JSON (`stream: false`):
```json
{
  "answer_id": "uuid",
  "answer_md": "FPT đạt Altman Z-Score 4.85 (Vùng An Toàn) [1] và hỗ trợ Fibonacci Golden Pocket tại 126,500 đ [2].",
  "citations": [
    {"n": 1, "url": "https://…", "title": "…", "chunk_id": "uuid"},
    {"n": 2, "url": "https://…", "title": "…", "chunk_id": "uuid"}
  ],
  "sources": [ {"url": "…", "title": "…", "domain": "…"} ],
  "model": "gpt-4o-mini",
  "latency_ms": 4200,
  "status": "complete",
  "cached": false
}
```

### SSE stream (`stream: true`, `Accept: text/event-stream`)

| Event | Data | Ý nghĩa |
|---|---|---|
| `status` | `{"stage": "searching", "detail": "…"}` | Tiến trình pipeline |
| `engine_done` | `{"engine": "viet_stock", "results": 4, "ms": 15}` | Client thấy từng engine xong theo thời gian thực |
| `sources` | `{"sources": [...]}` | Danh sách nguồn sau merge (trước khi viết answer) |
| `token` | `{"t": "FPT đạt"}` | Token answer streaming |
| `citations` | `{"citations": [...]}` | Sau khi stream xong, đã validate |
| `done` | `{"answer_id": "…", "latency_ms": 4200, "status": "complete"}` | Kết thúc |
| `error` | `{"code": "…", "message": "…"}` | Lỗi giữa stream |

## 3. POST /v1/scrape

Request: `{"urls": ["https://…"], "timeout_s": 8}`
Response: mỗi URL 1 object `{url, status, markdown, title, extractor_used, error}`; URL lỗi → `{status: "failed", error: "…"}` (không fail cả batch).

## 4. Deep Research (`/v1/research`)

- `POST /v1/research` → `202 Accepted` + `{"research_id": "uuid", "status": "queued"}`
  - Body: `{query, lang, breadth (2..5), depth (1..5), max_time_s (10..600), k (1..20), profile ("auto" | "general" | "developer" | "academic" | "comparative" | "financial" | "news" | "fengshui" | "health")}`.
- `GET /v1/research/{id}` → `{research_id, status: queued|running|done|failed, progress: {iteration, max_iterations}, report_md, citations, learnings, cost_usd, error}` (hỗ trợ tự động khôi phục từ DB sau khi server restart).
- `GET /v1/research/{id}/events` → SSE: `status`, `plan`, `iteration_start`, `engine_done`, `learnings`, `report_token`, `citations`, `done`, `error`.

## 5. Automated Daily Briefings & Blog Digests (`/v1/digests`)

| Method | Path | Quyền | Mô tả |
|---|---|---|---|
| GET | `/v1/digests/topics` | `user`/`admin` | Danh sách 6 chủ đề quét tự động (AI Coding, AI Agents, Chứng khoán VN, Kinh tế vĩ mô, Phong thủy, Y tế sức khỏe) |
| GET | `/v1/digests` | `user`/`admin` | Danh sách bài phân tích blog mới nhất (lọc theo `topic`, `limit`; tự động fallback in-memory khi DB offline) |
| GET | `/v1/digests/{slug_or_id}` | `user`/`admin` | Chi tiết toàn văn bài phân tích Markdown + citations |
| POST | `/v1/digests/scan` | `admin` | Kích hoạt quét & tổng hợp bản tin mới ngay lập tức (body: `{"topic_key": "vn_finance_stocks" | null}`) |

## 6. Engine Management (`/v1/engines` — Hot Config)

| Method | Path | Quyền | Mô tả |
|---|---|---|---|
| GET | `/v1/engines` | `user`/`admin` | Danh sách 19 engines + config + health hiện tại |
| PATCH | `/v1/engines/{id}` | `admin` | Đổi `enabled`, `weight`, `timeout_ms`, `max_results` — hiệu lực ngay, không restart |
| GET | `/v1/engines/health` | `user`/`admin` | Success rate, p50/p95 latency, consecutive_failures, disabled_reason |

## 7. LLM Configuration & External System LLM Integration (`/v1/llm/*`, `/v1/context`, `/v1/citations/validate`)

| Method | Path | Quyền | Mô tả |
|---|---|---|---|
| GET | `/v1/llm/providers` | `user`/`admin` | Danh mục 20 provider presets (tổng hợp từ `OpenRouter Free Router`, `nejib1/Free-LLM`, và `awesome-freellm-apis`: `openrouter`, `pollinations`, `gemini`, `groq`, `nvidia_nim`, `cerebras`, `sambanova`, `mistral`, `github_models`, `cloudflare`, `cohere`, `chutes`, `siliconflow`, `hyperbolic`, `huggingface`, `ollama`, `custom`, `openai`, `anthropic`, `none`) + cấu hình đang kích hoạt |
| GET | `/v1/llm/config` | `user`/`admin` | Xem cấu hình LLM hiện tại (API key đã được che mặt nạ `sk-o...xxxx`) |
| PATCH | `/v1/llm/config` | `admin` | Cập nhật nóng `provider`, `protocol`, `base_url`, `api_key`, `quick_model`, `report_model`, `fallback_provider`, `extra_headers` ngay lập tức không cần restart |
| POST | `/v1/llm/config/reset` | `admin` | Khôi phục cấu hình LLM về mặc định từ biến môi trường `.env` |
| POST | `/v1/llm/test` | `user`/`admin` | Gửi gói tin kiểm tra kết nối và đo độ trễ `latency_ms` tới LLM endpoint |
| POST | `/v1/llm/council` | `user`/`admin` | **Hội Đồng Phán Quyết Đa Endpoint (LLM Council + Cross-Review + Dream-RSI)**: Chạy song song nhiều free endpoints → ẩn danh `Candidate A, B, C...` → Cross-Review chấm chéo → Chủ tịch tổng hợp phán quyết tối hậu |
| PATCH | `/v1/llm/council/endpoints` | `admin` | Cập nhật nóng danh sách các endpoint LLM phục vụ Hội đồng Phán quyết |
| GET | `/v1/llm/dream-rsi/state` | `user`/`admin` | Xem trạng thái Replay Buffer, điểm UCB1, thế hệ chính sách và ranking của các endpoint |
| POST | `/v1/llm/dream-rsi/reset` | `admin` | Xóa bộ nhớ quỹ đạo Dream-RSI để huấn luyện lại từ đầu |
| GET | `/v1/domains` | `user`/`admin` | Danh mục 8 miền chuyên sâu Universal Intelligence (`financial` Flagship Pilot + 7 verticals) |
| POST | `/v1/domains/analyze` | `user`/`admin` | Phân tích và dự kiến routing miền chuyên sâu, rubric thẩm định và prompt mẫu cho truy vấn |
| POST | `/v1/context` | `user`/`admin` | **Context-Only RAG cho hệ thống ngoài đã có sẵn LLM**: chạy 19 search engines + BCTC Forensics/Technicals + Scrape + Chunk + Rerank → trả về `{query_id, normalized_query, inferred_categories, context_md, system_prompt, messages, chunks, sources, latency_ms}` |
| POST | `/v1/citations/validate` | `user`/`admin` | **Hậu kiểm trích dẫn cho LLM ngoài**: nhận `{answer_md, chunks}` từ LLM của hệ thống đối tác, loại bỏ citation giả mạo và trả về `{validated_answer_md, citations, invalid_markers}` |

Ngoài ra, `POST /v1/answer` và `POST /v1/research` hỗ trợ trường tùy chọn `llm` (`LLMOverride`) để ghi đè `provider`, `base_url`, `api_key`, `model`, `extra_headers` theo từng request.

## 8. Operations & Debug

- `GET /healthz` → liveness; `GET /readyz` → check DB/Redis/SearXNG (tự động thích ứng Lite profile).
- `GET /metrics` → Prometheus metrics (`metasearch_http_requests_total`, `metasearch_http_request_duration_seconds`, `metasearch_cache_events_total`, ...).
- `GET /v1/ops/status` → tổng hợp trạng thái giants, 19 engines, DB/Redis.
- `GET /v1/queries` & `GET /v1/queries/{id}` → lịch sử và chi tiết query (graceful fallback khi DB offline).

## 9. Error model (chuẩn RFC 7807-ish)

```json
{
  "error": {
    "code": "rate_limited",           // invalid_request | unauthorized | rate_limited | upstream_timeout | no_results | internal
    "message": "…",
    "details": {"engines_status": {"searxng": "timeout", "tavily": "ok"}},
    "request_id": "uuid"
  }
}
```

HTTP mapping: 400 `invalid_request` · 401/403 `unauthorized` · 404 `not_found` · 429 `rate_limited` · 502 `upstream_timeout` · 503 `no_results` · 500 `internal`.

## 9. Rate limit & quotas

- Default: 60 req/phút/api_key (sliding window Redis ZSET, TTL `window_s + 1`, in-process fallback khi không có Redis).
- Header phản hồi trên mọi request xác thực: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, và `Retry-After` khi 429.
- Deep research: tối đa 5 job đồng thời.


## v2.3 / v2.4 additions (docs/18, docs/19 — audit round 11)

New request params (on `/v1/search` + `/v1/answer` unless noted):
- `deep: bool = false` — fan out to ALL enabled engines (category filter off,
  quorum early-return bypassed, still bounded by the fan-out budget). Cache
  keys include `deep` both ways. Precedence: explicit `engines` wins over
  `deep`.
- `lang: string` — `vi`/`en`/auto-omitted; reaches google_news (hl/gl/ceid),
  gdelt (`sourcelang:`), openalex (`language:`), searxng.
- `max_age_s: int` (AnswerRequest only, 30..86400) — DAT-10 valve: financial
  answers become cacheable with `ttl = min(answer_ttl, max_age_s)`; L2
  semantic cache refuses financial/news rows in both directions.
- `freshness_days`, `news`, `include_domains`/`exclude_domains` — see §1.

New endpoints:
- `POST /v1/fa/compare` `{symbols: string[1..20] (each ≤15 chars), timeout_ms?}`
  — cross-source fundamentals per symbol (provenance per field). Cost 3+N.
- `POST /v1/market/ohlcv` `{symbol, days? (clamped to market_ohlcv_min/max_days)}`
  — daily candles (VNDIRECT primary, SSI fallback, shared strict parser). Cost 2.
- `GET/POST/DELETE /v1/watchlist`, `GET /v1/watchlist/alerts` — per-key
  watchlist (cap 20) + poll-on-demand alerts. Cost 1 / 1+N/4.
- `POST /v1/tts/synthesize`, `GET /v1/tts/capabilities` — VoiceStudio server
  TTS (optional, VOICESTUDIO_URL). Cost 5 / 1.
- `POST /v1/podcast/briefing`, `GET /v1/podcast/briefing/status|latest|feed.xml`
  — daily audio briefing (optional VoiceStudio). Cost 5 / 1.
- `POST /v1/research`, `GET /v1/research/{id}`, `GET /v1/research/{id}/events`
  (SSE) — Deep Research job lifecycle (FR-9).

Rate-limit weights (auth on): answer 5, context 3, research 20,
tts/synthesize 5, podcast/briefing 5, fa/compare 3+symbols, market/ohlcv 2,
watchlist/alerts 1+items/4, everything else 1.
