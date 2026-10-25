# 06 — API Specification

Base URL: `http://localhost:8000` · Auth: `Authorization: Bearer <api_key>` (Phase 3; Phase 1–2 open local).

## 1. POST /v1/search — Raw search

Request:
```json
{
  "query": "so sánh RAG và fine-tuning",
  "lang": "vi",
  "k": 10,
  "engines": ["searxng", "tavily"],
  "categories": ["general", "academic"],
  "freshness_days": 90,
  "include_snippets": true
}
```

Response 200:
```json
{
  "query_id": "uuid",
  "results": [
    {
      "rank": 1,
      "url": "https://…",
      "title": "…",
      "snippet": "…",
      "score": 0.87,
      "consensus": 3,
      "found_by": ["searxng", "tavily"],
      "domain": "example.com",
      "published_at": "2025-11-02T00:00:00Z"
    }
  ],
  "engines_used": ["searxng", "tavily"],
  "engines_failed": [],
  "latency_ms": 1850,
  "cached": false
}
```

## 2. POST /v1/answer — Quick answer (JSON hoặc SSE)

Request: như `/v1/search` + `"stream": true|false`, `"model": "gpt-4o-mini"` (optional).

Response JSON (`stream: false`):
```json
{
  "answer_id": "uuid",
  "answer_md": "RAG phù hợp khi … [1]. Fine-tuning phù hợp khi … [2].",
  "citations": [
    {"n": 1, "url": "https://…", "title": "…", "chunk_id": "uuid"},
    {"n": 2, "url": "https://…", "title": "…", "chunk_id": "uuid"}
  ],
  "sources": [ {"url": "…", "title": "…", "domain": "…"} ],
  "model": "gpt-4o-mini",
  "latency_ms": 5400,
  "status": "complete"
}
```

### SSE stream (`stream: true`, `Accept: text/event-stream`)

| Event | Data | Ý nghĩa |
|---|---|---|
| `status` | `{"stage": "searching", "detail": "…"}` | Tiến trình pipeline |
| `engine_done` | `{"engine": "searxng", "results": 12, "ms": 940}` | Mượn từ multi-search-aggregator: client thấy từng engine xong |
| `sources` | `{"sources": [...]}` | Danh sách nguồn sau merge (trước khi viết answer) |
| `token` | `{"t": "RAG phù hợp"}` | Token answer streaming |
| `citations` | `{"citations": [...]}` | Sau khi stream xong, đã validate |
| `done` | `{"answer_id": "…", "latency_ms": 5400, "status": "complete"}` | Kết thúc |
| `error` | `{"code": "…", "message": "…"}` | Lỗi giữa stream |

## 3. POST /v1/scrape

Request: `{"urls": ["https://…"], "markdown": true, "timeout_s": 8}`
Response: mỗi URL 1 object `{url, status, markdown, title, extractor_used, fetched_at}`; URL lỗi → `{status: "failed", error: "…"}` (không fail cả batch).

## 4. Deep Research

- `POST /v1/research` → `202 Accepted` + `{"research_id": "uuid", "status": "queued"}` (body: query, breadth, depth, budget).
- `GET /v1/research/{id}` → `{status: queued|running|done|failed, progress: {iteration, max_iterations}, report_md, citations, learnings, cost_usd}`.
- `GET /v1/research/{id}/events` → SSE: `plan`, `iteration_start`, `engine_done`, `learnings`, `report_token`, `done`.

## 5. Engine management (hot config, mượn từ multi-search-aggregator)

| Method | Path | Mô tả |
|---|---|---|
| GET | `/v1/engines` | Danh sách engine + config + health hiện tại |
| PATCH | `/v1/engines/{id}` | Đổi `enabled`, `weight`, `timeout_ms`, `max_results` — hiệu lực ngay, không restart |
| GET | `/v1/engines/health` | Success rate, p50/p95 latency, consecutive_failures, disabled_reason |

## 6. Khác

- `GET /healthz` → liveness; `GET /readyz` → check DB/Redis/SearXNG.
- `GET /metrics` → Prometheus.
- `GET /v1/queries/{id}` → chi tiết 1 query (debug/eval).

## 7. Error model (chuẩn RFC 7807-ish)

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

HTTP mapping: 400 invalid_request · 401 unauthorized · 429 rate_limited · 502 upstream_timeout · 503 no_results/all engines down · 500 internal.

## 8. Rate limit & quotas

- Default: 60 req/phút/api_key (sliding window Redis).
- Header phản hồi: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `Retry-After` khi 429.
- Deep research: tối đa 5 job đồng thời/user.
