# 01 — Product Requirements (PRD)

## 1. Vision

> "Một API duy nhất cho mọi nhu cầu tra cứu web của AI Agent và ứng dụng: tìm đa nguồn, đọc nội dung, trả lời có trích dẫn — nhanh, rẻ, kiểm chứng được."

## 2. Personas

| # | Persona | Bối cảnh | Nỗi đau | MetaSearch giải quyết |
|---|---|---|---|---|
| P1 | **AI Agent Developer** | Xây chatbot/agent, cần tool tra cứu web | Tự gọi 3–4 API search + scraper, xử lý chặn IP, dedup, citation | MCP tools + REST API, một call thay cả chuỗi |
| P2 | **Researcher / Analyst** | Viết báo cáo tổng hợp từ nhiều nguồn | Đọc thủ công 20+ trang, khó truy vết nguồn | Deep Research: báo cáo có citation map về chunk |
| P3 | **End user (Web UI)** | Hỏi đáp như Perplexity | Google trả link, không trả câu trả lời | Web UI streaming answer + nguồn hiển thị |
| P4 | **Platform / DevOps** | Vận hành hệ thống | Không biết engine nào chết, chi phí LLM bao nhiêu | Engine health, metrics, cost dashboard, budget cap |

## 3. Use cases chính

- **UC-1 Quick Answer**: user hỏi → hệ thống search → scrape top-K → trả câu trả lời streaming có `[n]` citations (≤ 6s).
- **UC-2 Raw Search**: agent cần danh sách link đã dedup + scored, tự xử lý tiếp (≤ 2.5s).
- **UC-3 Scrape**: agent có sẵn URL → lấy Markdown sạch (≤ 3s, có SSRF guard).
- **UC-4 Deep Research**: câu hỏi phức tạp → planner tách sub-queries → vòng lặp search/scrape/learnings → báo cáo dài có cấu trúc (30–180s, async job).
- **UC-5 MCP Integration**: Claude/IDE gọi tool `web_search_and_scrape` trong phiên làm việc.

## 4. Functional Requirements

| ID | Yêu cầu | Priority | Phase |
|---|---|---|---|
| FR-1 | Search đa nguồn qua SearXNG (70+ engines) với 1 request | Must | 1 |
| FR-2 | Adapter cho Tavily / Serper / Brave (API key tùy chọn, fail-soft) | Must | 1 |
| FR-3 | Vertical engines: arXiv, Stack Overflow, Reddit, Wikipedia | Should | 2 |
| FR-4 | Dedup URL (canonicalize + near-dup SimHash) + consensus scoring | Must | 1 |
| FR-5 | Domain diversity cap (≤ 2–3 kết quả/domain) | Must | 1 |
| FR-6 | Scrape + extract → Markdown (trafilatura → crawl4ai → Playwright → jina fallback chain) | Must | 1 |
| FR-7 | Rerank heuristic-first (0ms); tuỳ chọn int8 ONNX ~300MB hoặc API rerank (Jina/Cohere) | Must | 2 |
| FR-8 | Quick answer streaming (SSE) với citations `[n]` map về chunk | Must | 1–2 |
| FR-9 | Deep Research async job (planner → loop → report) | Should | 3 |
| FR-10 | MCP Server 4 tools: `web_search`, `web_scrape`, `web_search_and_scrape`, `deep_research` | Should | 3 |
| FR-11 | Engine registry hot-config: bật/tắt engine, chỉnh weight không restart | Should | 2 |
| FR-12 | Engine health tracking + auto-disable engine lỗi liên tiếp | Should | 2 |
| FR-13 | Cache 2 tầng: Redis (TTL) + pgvector semantic cache | Should | 2–3 |
| FR-14 | Auth API key + rate limit per key | Must | 3 |
| FR-15 | Web UI: search box, streaming answer, nguồn, engine filter | Should | 2 |
| FR-16 | Hỗ trợ tiếng Việt: language param, reranker multilingual, VN news engines | Must | 1 |

## 5. Non-Functional Requirements

| ID | Yêu cầu | Target |
|---|---|---|
| NFR-1 | Latency quick answer | p50 ≤ 6s, p95 ≤ 12s |
| NFR-2 | Latency raw search | p95 ≤ 2.5s |
| NFR-3 | Availability | 99.5% (single node), degrade-soft khi engine lỗi |
| NFR-4 | Throughput | 20 RPS sustained trên 4 vCPU/8GB |
| NFR-5 | Chi phí | ≤ $0.005/query quick; deep research ≤ $0.30/report |
| NFR-6 | Bảo mật | SSRF guard, secrets không commit, rate limit |
| NFR-7 | Observability | Trace per stage, metrics, LLM call logging |
| NFR-8 | Khả năng mở rộng | Thêm engine mới ≤ 1 file + 1 dòng đăng ký |
| NFR-9 | Tiếng Việt | Đúng ngữ pháp VN trong answer; reranker hiểu VN |
| NFR-10 | Chạy trên laptop 16GB RAM (dev) | Stack ≤ 4GB trong WSL2 6GB; không chạy model lớn local (xem [ADR-008](adr/ADR-008-hardware-inference.md)) |

## 6. Out of scope (v1)

- Tự lập chỉ mục web (index crawler riêng) — dùng engine có sẵn.
- Personalization / lịch sử cá nhân hoá kết quả.
- Multi-tenant billing, marketplace plugins.
- Tìm kiếm hình ảnh/video đầy đủ (chỉ text + news).

## 7. Success metrics (sau 8 tuần)

- Golden set eval: citation accuracy ≥ 90%, faithfulness ≥ 0.85.
- 1 demo Web UI + 1 demo MCP client (Claude Desktop) chạy end-to-end.
- Load test k6: 20 RPS trong 10 phút, error < 1%.
- Chi phí thực tế trên 1.000 query mẫu ≤ $5.
