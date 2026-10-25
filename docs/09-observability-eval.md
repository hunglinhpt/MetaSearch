# 09 — Observability & Evaluation

## 1. Tracing (OpenTelemetry)

- 1 trace per request; **1 span per pipeline stage** (11 stage ở [03](03-search-pipeline.md)) + span con per engine call + per LLM call.
- Propagate `request_id` qua headers → logs → traces.
- Exporter: OTLP → collector → (Jaeger/Tempo) hoặc Langfuse cho LLM-specific.

Span attributes quan trọng:
```
metasearch.query, metasearch.lang, metasearch.intent
metasearch.engine.id, metasearch.engine.results, metasearch.engine.ms
metasearch.scrape.url, metasearch.scrape.extractor, metasearch.scrape.status
llm.model, llm.input_tokens, llm.output_tokens, llm.cost_usd
```

## 2. Metrics (Prometheus)

| Metric | Loại | Nhãn |
|---|---|---|
| `metasearch_requests_total` | counter | endpoint, status |
| `metasearch_request_duration_seconds` | histogram | endpoint |
| `metasearch_stage_duration_seconds` | histogram | stage |
| `metasearch_engine_duration_seconds` | histogram | engine |
| `metasearch_engine_success_total` / `_failures_total` | counter | engine, reason |
| `metasearch_cache_hits_total` | counter | layer (l1/l2) |
| `metasearch_llm_tokens_total` | counter | model, direction |
| `metasearch_llm_cost_usd_total` | counter | model |
| `metasearch_citations_valid_ratio` | gauge | — |
| `metasearch_scrape_success_ratio` | gauge | extractor |

## 3. LLM observability (Langfuse)

- Trace per answer/research: prompt, completion, tokens, cost, latency, model.
- Gắn `session_id`, `query_id`, `user_feedback` (👍/👎 từ Web UI).
- Dùng để: debug prompt, so sánh model, tính cost theo persona.

## 4. Logging

- Structured JSON stdout: `ts, level, request_id, stage, msg, extra`.
- Không log nội dung page đầy đủ (chỉ url + hash); PII trong query được mask trước khi log.

## 5. Evaluation harness

### Golden set
- 50–100 query (60% vi, 40% en) chia theo intent: factual, exploratory, code, news, academic.
- Mỗi query có: expected sources (domain list), key facts phải xuất hiện.

### Metrics tự động (chạy nightly trên golden set)

| Metric | Cách đo | Target |
|---|---|---|
| Citation accuracy | % citation `[n]` map đúng chunk chứa claim | ≥ 90% |
| Faithfulness | RAGAS faithfulness (answer vs context) | ≥ 0.85 |
| Answer relevance | RAGAS answer relevancy | ≥ 0.8 |
| Retrieval hit@10 | % query có ≥1 expected domain trong top-10 | ≥ 80% |
| Latency p50/p95 | từ traces | ≤ 6s / ≤ 12s |
| Cost/query | từ LLM metering | ≤ $0.005 |

### Quy trình
1. `make eval` → chạy harness (pytest + ragas) trên golden set → xuất JSON + trend.
2. Regression gate: metric giảm > 3% so với baseline → chặn merge (CI optional Phase 4).
3. Mỗi thay đổi prompt/reranker/model → chạy eval trước khi ship.

## 6. Dashboards (Grafana)

- **Overview**: RPS, p50/p95 per endpoint, error rate.
- **Engines**: success rate, latency per engine, disabled engines.
- **Cost**: tokens + USD per model per ngày; cost per query p50.
- **Quality**: citation valid ratio, scrape success ratio, cache hit rate.
