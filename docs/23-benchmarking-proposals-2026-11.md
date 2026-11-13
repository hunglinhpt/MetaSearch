# 23 — Benchmarking & Feature Proposals (Phase 2-3 của audit lifecycle 2026-11-14)

> Đầu vào: benchmark 3 nguồn (research / OSS / commercial) + 14 vòng audit
> của toàn hệ thống. Kết luận và scope-lock cho các wave tiếp theo.

## 1. Benchmark sources

### 1.1 Research & algorithms
- **RAG Triad (TruLens)**: context relevance / groundedness / answer relevance —
  3 trụ đánh giá RAG end-to-end. MetaSearch đã có P0-4 (claim-level citation
  check) = groundedness; **thiếu answer-relevance và context-relevance scoring**.
- **RAGAS** (de facto standard): faithfulness + answer relevancy + context
  precision/recall. Eval harness hiện tại (eval_v2) đo hit@5/citation — chưa có
  LLM-as-judge.
- arXiv 2405.13622 (task-specific accuracy), TREC 2025 RAG track: benchmark
  retrieval tách khỏi generation.

### 1.2 Open-source
- **SearXNG**: retrieval layer thuần (70-269 engines) — MetaSearch đã giants-first
  trên đúng nền này.
- **Perplexica**: single-pass search + summarize — MetaSearch vượt qua (IRCoT
  loop + coverage gate + hybrid search + Spaces).
- **MindSearch (InternLM)**: planner/searcher decomposition dạng graph-of-queries
  — MetaSearch IRCoT là dạng tuyến tính; graph-of-queries là nâng cấp tự nhiên.
- **DeepSearcher (Zilliz)**: agentic RAG iterative local — pattern tương tự IRCoT.

### 1.3 Commercial (Perplexity Pro / Deep Research)
- Inline citations trên MỌI claim ✓ (MetaSearch đã có `[n]` + claim check).
- Internal Search (web + team files) — MetaSearch có Spaces + crawl ✓ tương đương.
- **Không có watchlist/alerts** — MetaSearch đã có sẵn → lợi thế cạnh tranh.
- **Export/PDF/CSV**: Perplexity hạn chế → cơ hội khác biệt hóa cho Compare table.

## 2. Proposals + critical review

| # | Proposal | Benchmark ref | Giá trị | Đánh giá | Scope |
|---|---|---|---|---|---|
| 1 | **CSV export cho /v1/fa/compare + Watchlist** | Perplexity không có export; commercial research workflows cần data exit | Feasible ~80 dòng; ROI cao; zero risk | **P0 — implement ngay** | ✅ |
| 2 | **RAGAS-lite judge** (answer relevance + context precision) vào eval_v2 | TruLens RAG Triad, RAGAS | Nâng eval harness lên chuẩn ngành | Feasible (1 LLM call/query); có sẵn harness | **P1 — next release** |
| 3 | **SSE push cho watchlist alerts** | Perplexity không có alerts (lợi thế) | Real-time UX; đã có hạ tầng SSE | Feasible; cần connection mgmt per key | **P1 — next release** |
| 4 | Graph-of-queries planner (MindSearch) | MindSearch paper | Deep research tốt hơn cho câu hỏi phân nhánh | Complexity cao; IRCoT tuyến tính đã đủ tốt | ❌ P2 — deferred |
| 5 | Request-body size cap middleware | OWASP best practice | Chặn DoS body 100MB trước parse | ~20 dòng middleware; zero risk | **P1 — next release** |
| 6 | ColPali multimodal PDF | docs/20 wave P2 | Vision PDF retrieval | Over-engineered cho hiện tại | ❌ P2 — deferred |
| 7 | KG LightRAG theo space | LightRAG | Knowledge graph per space | Cấu trúc dữ liệu mới + chi phí lớn | ❌ P2 — deferred |

**Scope-lock Phase 4**: chỉ #1 (P0) được implement trong batch này — P1 #2/#3/#5
đã ghi vào docs/12 roadmap cho release kế tiếp (đúng quy trình scope-lock:
discard over-engineered #4/#6/#7).

## 3. Implement #1 — CSV export

- `GET /v1/fa/compare?format=csv` — cùng body, trả `text/csv` với BOM UTF-8
  (Excel VN), header row symbol/name/exchange + cột metric, ô rỗng cho missing,
  cột `sources` liệt kê provenance.
- Tests: happy path 2 mã, missing field → ô rỗng, vẫn giữ JSON khi không có
  `format=csv`.
