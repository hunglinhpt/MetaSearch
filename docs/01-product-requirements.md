# 01 — Product Requirements (PRD)

## 1. Vision

> "Một API duy nhất cho mọi nhu cầu tra cứu web, phân tích tài chính định lượng và nghiên cứu chuyên sâu của AI Agent và ứng dụng: tìm đa nguồn, thẩm định BCTC & kỹ thuật, đọc nội dung, trả lời có trích dẫn — nhanh, rẻ, kiểm chứng được."

## 2. Personas

| # | Persona | Bối cảnh | Nỗi đau | MetaSearch giải quyết |
|---|---|---|---|---|
| P1 | **AI Agent Developer** | Xây chatbot/agent, cần tool tra cứu web | Tự gọi 3–4 API search + scraper, xử lý chặn IP, dedup, citation | MCP tools + REST API, một call thay cả chuỗi |
| P2 | **Researcher / Financial Analyst** | Viết báo cáo tổng hợp và thẩm định doanh nghiệp | Đọc thủ công 20+ trang, chỉ có chỉ số thô (P/E, ROE), thiếu pháp y BCTC & kỹ thuật sâu | Deep Research + Financial Forensics (CAMELS, Altman Z, Beneish M, Piotroski F) + Quantitative Technicals |
| P3 | **End user (Web UI)** | Hỏi đáp như Perplexity & theo dõi bản tin hàng ngày | Google trả link, không trả câu trả lời; tin tức phân mảnh | Web UI streaming answer + nguồn hiển thị + Daily Briefings Blog |
| P4 | **Platform / DevOps** | Vận hành hệ thống | Không biết engine nào chết, chi phí LLM bao nhiêu | Engine health, metrics, cost dashboard, budget cap |

## 3. Use cases chính

- **UC-1 Quick Answer**: user hỏi → hệ thống search → scrape top-K → trả câu trả lời streaming có `[n]` citations (≤ 6s).
- **UC-2 Raw Search**: agent cần danh sách link đã dedup + scored, tự xử lý tiếp (≤ 2.5s).
- **UC-3 Scrape**: agent có sẵn URL → lấy Markdown sạch (≤ 3s, có SSRF guard + IP pinning).
- **UC-4 Deep Research**: câu hỏi phức tạp → planner tách sub-queries theo 8 `Harness²` profiles → vòng lặp search/scrape/learnings → báo cáo dài có cấu trúc (30–180s, async job, durable DB recovery).
- **UC-5 MCP Integration**: Claude/IDE gọi tool `web_search_and_scrape` trong phiên làm việc.
- **UC-6 Institutional Financial & Technical Analysis**: tra cứu mã cổ phiếu (HSX/HNX hoặc Quốc tế) → bóc tách pháp y BCTC theo ngành (CAMELS Ngân hàng, Margin/FVTPL Chứng khoán, Người mua trả tiền trước BĐS/KCN, Altman Z, Beneish M, Piotroski F) + chỉ báo kỹ thuật định lượng (Fibonacci Golden Pocket & Extensions, Ichimoku Cloud, True EMA MACD, Wilder's RSI/ATR, Kênh hồi quy $\pm 2\sigma$, 8 mẫu hình giá/nến).
- **UC-7 Automated Daily Briefings & Blog**: quét tự động hoặc theo yêu cầu (`/v1/digests`) các chủ đề AI Coding, AI Agents, Chứng khoán VN, Kinh tế vĩ mô, Phong thủy, Y tế sức khỏe.

## 4. Functional Requirements

| ID | Yêu cầu | Priority | Phase |
|---|---|---|---|
| FR-1 | Search đa nguồn qua SearXNG (70+ engines) + DuckDuckGo keyless fallback | Must | 1 |
| FR-2 | Adapter cho Tavily / Serper / Brave (API key tùy chọn, fail-soft) | Must | 1–3 |
| FR-3 | 14 Vertical engines (19 engines tổng cộng): `viet_stock` (HSX/HNX BCTC Forensics & Technicals), `stooq` (Global Macro/FX/Commodities), `yahoo_finance`, `sec_edgar`, `fengshui` (Bát Trạch/Can Chi vạn niên), `google_news`, `arxiv`, `pubmed`, `semanticscholar`, `github`, `huggingface`, `stackoverflow`, `reddit`, `wikipedia` | Must | 2–5 |
| FR-4 | Dedup URL (canonicalize + near-dup SimHash 64-bit) + consensus scoring | Must | 1–2 |
| FR-5 | Domain diversity cap (≤ 2 kết quả/domain mặc định; ≤ 4 cho các bộ máy định lượng đa chiều `viet_stock`, `stooq`, `fengshui`, `sec_edgar`) | Must | 1–5 |
| FR-6 | Scrape + extract → Markdown (`trafilatura` → `crawl4ai` → `jina` fallback chain, SSRF IP-pinning, Anti-CAPTCHA escalation) | Must | 1–5 |
| FR-7 | Rerank heuristic-first (0ms); tuỳ chọn int8 ONNX ~300MB hoặc API rerank (Jina/Cohere) | Must | 2 |
| FR-8 | Quick answer streaming (SSE) với citations `[n]` map về chunk | Must | 1–2 |
| FR-9 | Deep Research async job (planner → loop → report, 8 `Harness²` profiles, durable DB recovery) | Should | 3–5 |
| FR-10 | MCP Server 4 tools: `web_search`, `web_scrape`, `web_search_and_scrape`, `deep_research` | Should | 3 |
| FR-11 | Engine registry hot-config: bật/tắt engine, chỉnh weight không restart (`PATCH /v1/engines/{id}`, RBAC admin) | Should | 2–5 |
| FR-12 | Engine health tracking + auto-disable engine lỗi liên tiếp + recovery probe | Should | 2 |
| FR-13 | Cache 2 tầng: Redis (TTL + in-process fallback) + pgvector semantic cache | Should | 2–3 |
| FR-14 | Auth API key (`msk_*`) + RBAC (`admin`/`user`) + sliding-window rate limit per key | Must | 3–5 |
| FR-15 | Web UI: search box, category chips, streaming answer, Markdown tables, nguồn, Daily Briefings Blog | Should | 2–5 |
| FR-16 | Hỗ trợ tiếng Việt: language param, VN stopwords, Smart Category Routing tiếng Việt | Must | 1–5 |

## 5. Non-Functional Requirements

| ID | Yêu cầu | Target |
|---|---|---|
| NFR-1 | Latency quick answer | p50 ≤ 6s, p95 ≤ 12s |
| NFR-2 | Latency raw search | p95 ≤ 2.5s |
| NFR-3 | Availability | 99.5% (single node), degrade-soft khi engine/DB/Redis lỗi |
| NFR-4 | Throughput | 20 RPS sustained trên 4 vCPU/8GB |
| NFR-5 | Chi phí | ≤ $0.005/query quick; deep research ≤ $0.30/report |
| NFR-6 | Bảo mật | SSRF IP-pinning + redirect re-check, RBAC admin, secrets không commit, rate limit |
| NFR-7 | Observability | Trace per stage, Prometheus metrics, Langfuse LLM call logging |
| NFR-8 | Khả năng mở rộng | Thêm engine mới ≤ 1 file + 1 dòng đăng ký |
| NFR-9 | Tiếng Việt | Đúng ngữ pháp VN trong answer; hỗ trợ thuật ngữ tài chính/phong thủy VN |
| NFR-10 | Chạy trên laptop 16GB RAM (dev) | Stack ≤ 4GB trong WSL2 6GB (Lite profile không Docker ~100MB RAM) |

## 6. Out of scope (v1)

- Tự lập chỉ mục web (index crawler riêng) — dùng engine có sẵn.
- Personalization / lịch sử cá nhân hoá kết quả.
- Multi-tenant billing, marketplace plugins.
- Tìm kiếm hình ảnh/video đầy đủ (chỉ text + news).

## 7. Success metrics (v1.0.0)

- Golden set eval (40 queries): citation accuracy ≥ 90%, faithfulness ≥ 0.85 (`--judge-faithfulness`).
- 333 unit tests PASS, `ruff` 0 lỗi, `bandit` 0 lỗi bảo mật, Next.js build sạch.
- Load test k6: 20 RPS trong 10 phút, error < 1%.
- Chi phí thực tế trên 1.000 query mẫu ≤ $5.
