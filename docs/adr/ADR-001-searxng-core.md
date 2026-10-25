# ADR-001 — SearXNG làm meta-search core

**Status:** Accepted · **Date:** 2025 · **Liên quan:** [docs/02 §2](../02-system-architecture.md), [docs/03 stage 5](../03-search-pipeline.md)

## Context

Cần gom kết quả từ Google, Bing, DuckDuckGo + 70 engine khác cho 1 truy vấn. Tự viết adapter từng engine (kiểu gpt-researcher) tốn công bảo trì lớn: engine đổi HTML/API là vỡ, dễ bị chặn IP/captcha, và phải tự xử lý dedup + consensus ranking.

## Decision

Self-host **SearXNG** làm unified search API (bật `formats: [html, json]` trong `settings.yml`, limiter off vì nội bộ). `SearXNGAdapter` là adapter primary; Tavily/Serper/Brave chỉ là adapter phụ tuỳ chọn (Engine Registry, hot-config qua `PATCH /v1/engines/{id}`).

## Rationale

- 1 request → SearXNG tự fan-out 70+ engine, khử trùng lặp URL, tính điểm đồng thuận, trả JSON sạch.
- $0 phí tầng search; cộng đồng SearXNG bảo trì parser engine liên tục.
- Engine đổi cấu trúc → chỉ nâng image SearXNG, orchestration không đổi.

## Alternatives

| Phương án | Vì sao không chọn |
|---|---|
| Tự viết adapter từng engine | Gánh nặng bảo trì parser + chống block; 1 người maintain không đủ |
| API thương mại làm primary (Serper/Tavily) | Phí theo query, lock-in, thiếu đa nguồn |
| Tự vận hành aggregator 30+ engine (chen150450/multi-search-aggregator) | Vẫn phải tự giữ parser sống; chỉ mượn ý tưởng Engine Registry hot-config |

## Consequences

- Phụ thuộc 1 service Docker (~512MB RAM) + hop nội bộ +50–150ms.
- Bị block IP → cấu hình limiter/proxy tuỳ chọn; theo dõi engine success rate ≥95%.
- Một số engine SearXNG mặc định không cần → tắt trong settings để giảm latency.
