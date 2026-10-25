# ADR-008 — Ràng buộc phần cứng 16GB & chiến lược inference

**Status:** Accepted · **Date:** 2025 · **Liên quan:** [docs/01 NFR-10](../01-product-requirements.md), [docs/08 §1](../08-deployment.md), [docs/03 stage 10](../03-search-pipeline.md)

## Context

Máy dev: laptop 16GB (15.7GB usable), Windows 11 + trình duyệt + IDE đã chiếm 6–8GB, GPU Intel Iris Xe (dùng chung RAM, không VRAM rời). WSL2 mặc định chiếm 50% RAM host. Chạy model lớn local sẽ đè stack Docker.

## Decision

**RAM:** WSL2 cap 6GB (`%UserProfile%\.wslconfig`: `memory=6GB, processors=4, swap=2GB`); stack Docker ≤4GB — searxng 512m, redis 256m, postgres 768m, api 512m, crawl4ai 1.5g, frontend 256m; headroom ~2GB.

**Inference:**

| Thành phần | Quyết định | Vì sao |
|---|---|---|
| LLM | Cloud API mặc định (gpt-4o-mini/claude-haiku cho intent+synthesize; gpt-4o/claude-sonnet cho report) | 7B–8B local cần 4–8GB → đè stack |
| Ollama | Chỉ 3B q4, experimental offline, **tắt mặc định** | Chất lượng thấp hơn cloud, chỉ dùng khi mất mạng |
| Reranker | Heuristic-first (0.5×consensus + 0.3×keyword + 0.2×freshness, 0ms); tuỳ chọn int8 ONNX multilingual ~300MB (`RERANKER=onnx`) hoặc API rerank (~$0.001/query) | bge-reranker-v2-m3 ~2.3GB + CPU 2–3s/query không phù hợp 16GB |
| Embedding (L2 cache) | e5-small int8 384 dims (~150MB) nếu bật; prod dùng bge-m3 1024 dims qua API | bge-m3 local ~2.2GB |
| Playwright | Chỉ trong crawl4ai service | Không cài Chromium vào image api |
| Iris Xe | Không dùng inference | iGPU dùng chung RAM, không có lợi thế |

## Rationale

Heuristic rerank đạt phần lớn giá trị với chi phí 0; phần còn lại là tuỳ chọn có thể bật khi có phần cứng tốt hơn — chỉ đổi env (`LLM_PROVIDER`, `RERANKER`), không đổi kiến trúc.

## Alternatives

| Phương án | Vì sao không chọn |
|---|---|
| Ollama 7B–8B local | 4–8GB RAM → chỉ chạy được khi tắt stack; chậm trên CPU |
| GPU cloud | Chi phí + phức tạp vận hành cho dev cá nhân |
| Nâng cấp RAM | Ngoài scope thiết kế; kiến trúc đã sẵn sàng mở rộng |

## Consequences

- Phụ thuộc API key + chi phí LLM (~$0.005/query quick answer, xem KPI [docs/12](../12-roadmap-plan.md)).
- Offline mode bị giới hạn nghiêm trọng (chỉ raw search, không answer).
- Khi có máy mạnh hơn: bật Ollama/vLLM + reranker local bằng env, không sửa code.
