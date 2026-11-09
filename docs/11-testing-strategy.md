# 11 — Testing Strategy

## 1. Nguyên tắc

- Pyramid: nhiều unit (engine adapter, scoring, SSRF guard) → ít integration (pipeline end-to-end với SearXNG thật) → e2e smoke (API + UI).
- Mọi test không gọi internet trừ khi đánh dấu `@pytest.mark.live` (chạy nightly, không chạy trong CI per-commit).
- Fixtures: SearXNG local qua docker; HTML mẫu (fixtures/) cho extractor tests.

## 2. Unit tests (pytest; CI chặn khi coverage `app` < 84%)

| Module | Test chính |
|---|---|
| `engines/searxng.py` | parse JSON, map fields, timeout → EngineError, partial results |
| `engines/registry.py` | load config, hot-reload PATCH, weight sort, disable engine lỗi |
| `merger.py` | canonicalize URL (utm strip, trailing slash), dedupe, consensus count, diversity cap |
| `scoring.py` | multi-signal formula: từng signal riêng + tổng; edge case (0 results, 1 engine) |
| `simhash.py` | near-duplicate: Hamming ≤ 3 gộp; khác → giữ |
| `scraper/` | trafilatura extract từ HTML fixture; fallback chain khi extractor 1 fail; SSRF block từng payload |
| `ssrf.py` | 20+ payload: metadata IP, localhost, redirect tới internal, DNS rebinding, IPv6, decimal IP |
| `reranker.py` | mock model: sort đúng theo score; batch; timeout → giữ thứ tự gốc |
| `cache.py` | L1 hit/miss, L2 semantic threshold 0.92, TTL, negative cache |
| `orchestrator/` | planner tách sub-queries; loop dừng đúng điều kiện (max_iter, đủ learnings, hết budget) |
| `llm_gateway.py` | routing theo intent; fallback khi provider lỗi; cost tính đúng |
| `citations.py` | map `[n]` → chunk; phát hiện citation không tồn tại → invalid |

## 3. Integration tests (docker compose test profile)

- `test_search_e2e`: POST /v1/search với SearXNG thật (local) → ≥ 1 result, latency < 5s.
- `test_answer_e2e`: /v1/answer stream=false → answer có ≥ 1 citation valid.
- `test_sse`: /v1/answer stream=true → nhận đủ event sequence `status → engine_done → sources → token* → citations → done`.
- `test_engine_failure`: tắt SearXNG → request vẫn trả kết quả từ engine phụ (graceful degradation).
- `test_hot_config`: PATCH /v1/engines/{id} → request sau dùng config mới không restart.
- `test_mcp`: gọi tool `web_search_and_scrape` qua MCP client → output markdown có citation.

## 4. E2E / smoke (k6 + Playwright)

- k6 script: 50 VU trong 2 phút trên /v1/search → p95 < 2.5s, error < 1%.
- k6 SSE: 20 VU /v1/answer stream → p95 TTFT < 3s.
- Playwright: Web UI nhập query → thấy sources + answer + citations render.

## 5. Security tests

- SSRF suite (như [10](10-security-compliance.md) §8) chạy trong CI — bắt buộc pass.
- Rate limit test; auth test (key sai → 401; key hết hạn → 401).
- gitleaks + pip-audit + npm audit trong CI.

## 6. Eval (chất lượng LLM) — xem chi tiết [09](09-observability-eval.md) §5

- Golden set 40 query, chạy tay (`--judge-faithfulness`). Chưa chạy nightly, chưa có RAGAS hay regression gate.

## 7. CI pipeline (GitHub Actions)

```yaml
jobs:
  lint:        ruff check + mypy (app/core, app/api) / tsc (frontend)
  unit:        pytest -m "not live" --cov=metasearch --cov-fail-under=80
  security:    gitleaks, pip-audit, npm audit, ssrf-suite
  integration: docker compose up test profile → pytest -m integration
  (nightly)    integration với compose, perf smoke, live smoke (chỉ báo cáo); eval chưa có
```
