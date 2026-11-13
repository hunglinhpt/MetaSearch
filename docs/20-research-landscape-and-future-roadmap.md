# docs/20 — Nghiên cứu landscape search/RAG 2026 & đề xuất roadmap chi tiết cho MetaSearch

> Phạm vi: (1) công trình học thuật về retrieval/RAG/deep-research, (2) open-source
> engine/framework tương đương, (3) sản phẩm thương mại (answer engines, search API
> cho agent). Mỗi đề xuất đều có: bằng chứng, cách tích hợp vào kiến trúc hiện tại
> (file/module cụ thể), effort, KPI, rủi ro. Quy ước ADR-007 (giants-first) được áp
> dụng cho mọi đề xuất: không tự viết cái đã có giant làm tốt.
>
> Research date: 2026-11-12. Nguồn: web search 2026 (arXiv/ACL anthology, techRxiv
> Deep-RAG survey 2026, jina/BAAI/Voyage/NVIDIA blogs, Brave/Kagi/Exa/Tavily/
> Perplexity docs, vectorize.io Mem0-vs-Zep, benchmarks BEIR/MTEB/MIRACL/TREC-RAG).

## 1. Bức tranh công trình (papers) — bài học cho MetaSearch

| Công trình / hướng | Phát hiện chính | Gap của MetaSearch hôm nay |
|---|---|---|
| **Late chunking** (Jina, 2024–25) | embed cả document dài rồi mới chunk vector thì giữ được ngữ cảnh; i.i.d. chunk làm mất context, rõ nhất với doc dài | `chunker.py` chunk rồi embed từng chunk (i.i.d.) — chưa late-chunking |
| **HyDE** (Gao et al. 2022) | query → LLM viết "hypothetical document" → embed cái đó để retrieve; widen recall tốt cho câu hỏi mơ hồ | chưa có query expansion bằng LLM; chỉ có multi-engine fan-out |
| **Multi-query + RRF fusion** (NirDiamant RAG_Techniques; production blogs 2026) | N biến thể query + Reciprocal Rank Fusion là baseline mạnh nhất về recall@precision | `merger.py` gộp theo consensus + heuristic signals, chưa phải RRF chuẩn với score normalization theo rank |
| **ColBERTv2 / PLAID / ColPali** (ECIR workshop; dataspire 2025) | late interaction (MaxSim token-level) thắng single-vector ở exact/multi-topic/OOD query; ColPali retrieve PDF page bằng ảnh, bỏ OCR | reranker của ta là heuristic/ONNX/API cross-encoder trên chunk text; chưa có late-interaction; chưa multimodal |
| **BGE-M3** (BAAI 2024, MIRACL) | một model cho dense + sparse (BM25-like) + multi-vector (ColBERT) — hybrid không cần thêm infra; đa ngữ (có vi) | ta dùng pgvector (dense only) + SearXNG (BM25 ở ngoài); chưa sparse/lexical trên chunk của chính mình |
| **Self-RAG / FLARE / Adaptive-RAG** (Asai 2023; Jeong 2024) | không phải query nào cũng cần retrieve; routing theo độ phức tạp query cải quality+latency | ta có `deep` flag do user bật; chưa có router tự動 phân loại độ phức tạp |
| **Deep-RAG survey 2026 (techRxiv 177272838)** + IRCoT/Search-o1/R1-Searcher/ReSearch | deep research = vòng lặp think→retrieve→reformulate có điều kiện trên bằng chứng đã thấy, có RL/CoT; entity-linking hop và query reformulation là 2戦略 chính trên HotpotQA/MuSiQue | `research.py` đã có planner/executor/synthesizer loop, nhưng chưa reformulate query dựa trên evidence từng hop + chưa có coverage check trước khi tổng hợp |
| **GraphRAG vs vanilla RAG** (GraphRAG-Bench 6/2025: graph KHÔNG phải lúc nào cũng thắng; LightRAG (MIT, EMNLP'25) graph+vector 2 tầng, incremental, chạy trên pgvector/Neo4j/Qdrant…) | graph chỉ lời ở global/summary query; vector vẫn xương sống cho fact retrieval | ta chưa có KG layer nào; watchlist/podcast là polling, chưa có graph memory |
| **Hallucination/citation detection** (RAGTruth 2024; survey Liu 2025; Vectara HHEM) | claim-level entailment (NLI) là cách rẻ và hiệu quả để phát hiện câu trả lời không được chứng minh bởi chunk | có `citations.validate_citations` (đếm/chéo URL) nhưng chưa kiểm entailment từng claim |
| **Đánh giá** (BEIR, MTEB, MIRACL, TREC 2024 RAG Track, RAGAS/ARES) | bộ chuẩn: BEIR zero-shot retrieval, MTEB embedding, MIRACL đa ngữ (có vi), TREC RAG end-to-end, RAGAS metrics (faithfulness, context precision/recall) | eval harness có 40 golden + judge faithfulness; chưa có BEIR-style retrieval metrics, chưa có context precision/recall, chưa có bộ vi riêng |
| **Vietnamese retrieval gap** (Findings of EACL 2026) | tiếng Việt là ngôn ngữ under-resourced về retrieval; tokenization ( underthesea/VnCoreNLP) ảnh hưởng lớn đến BM25 | có stopwords VN + lang, nhưng chưa có tokenizer-aware BM25 cho chỉ mục của chính mình |
| **Online eval** (A/B, interleaving; correlation studies) | offline metrics tương quan tốt với满意度 ở search đồng nhất, nhưng federated/vertical thì online mới đúng; interleaving rẻ hơn A/B cho reranker | chưa có online metrics/interleaving cho thay đổi reranker |

## 2. Open-source tương đương — nên đứng trên vai ai

| Nhóm | Dự án tiêu biểu | Nhận định cho MetaSearch |
|---|---|---|
| Meta-search engine | **SearXNG** (đang dùng), Yacy | giữ SearXNG; Yacy P2P không phù hợp server |
| Vector DB / hybrid search | **Qdrant, Weaviate, Vespa, Meilisearch, Typesense, Marqo, Infinity** | đang pgvector (đủ ở scale hiện tại); Vespa/Qdrant cân nhắc khi >100M chunk hoặc cần native hybrid+BM25 trong DB; **Marqo/Infinity** là tham chiếu tốt cho hybrid scoring nội bộ |
| Embedding/rerank models (open weights) | **BGE-M3, bge-reranker-v2-m3** (BAAI, Apache-2.0, đa ngữ có vi), **jina-embeddings-v3/v4 + jina-reranker-v3** (reranker listwise 0.6B, "last-but-not-late interaction"), Voyage voyage-3/rerank-2 (API), **NVIDIA NeMo Retriever NIMs** (Apache-2.0, self-host GPU: extraction, embedding, rerank) | đây là **đòn bẩy lớn nhất**: thay ONNX reranker hiện tại bằng bge-reranker-v2-m3 local (CPU int8 được) hoặc NIM nếu có GPU; BGE-M3 cho hybrid dense+sparse+multi-vector một model |
| Extraction/crawl | crawl4ai (đang dùng), **Firecrawl**, Jina Reader, **NeMo Retriever extraction NIM** (PDF layout, tables, charts) | thêm NeMo extraction NIM cho BCTC PDF (bảng/số liệu/biểu đồ) khi có GPU — quan trọng cho finance |
| RAG frameworks (nếu cần UI/engine có sẵn) | RAGFlow, Dify, Onyx/Danswer, Haystack, LangChain/LlamaIndex | **không cần** — ta có pipeline riêng; chỉ tham khảo thiết kế (RAGFlow: parsing theo layout + citation UI) |
| Memory / KG cho agent | **Graphiti (Zep, Apache-2.0)** temporal KG incremental; **Mem0** (vector+graph); HippoRAG (neuro-inspired indexing, research) | Graphiti là ứng viên số 1 cho memory theo API key + LightRAG-style KG cho "Spaces" theo domain |
| Agentic search APIs | **Tavily** (search/extract/crawl/map + agent skills), **Exa** (neural + category filters + highlights/contents), Brave (Goggles rerank rules), Serper | chuẩn đầu ra để convertirse thành "search API cho agent": thêm `contents`/`highlights` vào `/v1/search` |

## 3. Sản phẩm thương mại — tính năng đáng học (2026)

| Sản phẩm | Tính năng nổi bật | Bài học |
|---|---|---|
| **Perplexity** | answer engine + citations; Spaces (bộ sưu tập theo chủ đề); follow-up questions gợi ý; Comet/Computer (agent browser); Pro search: source filters, time filters, "Labs" reports | follow-up suggestions + Spaces + source/time filter ở UI — rẻ, giá trị cao |
| **Kagi** | Lenses (bộ lọc/lift tuỳ biến), personal results (loại tên miền của chính user), Quick Peek, small web | **custom rerank rules theo user** (xem §4 P6) |
| **Brave** | Goggles (luật rerank công khai: boost/demote site, từ khoá — trực tiếp trên kết quả), independent index, Goggles trong API | rerank rules có thể công khai/chia sẻ — khác biệt lớn |
| **Exa** | neural search, `contents` (text/markdown), `highlights` (đoạn liên quan nhất trong page), category/date/domain filters, JSON structured output | thêm `highlights` + `contents` + structured extraction vào API của ta |
| **Tavily** | search với `search_depth: deep`, extract (đọc 1 URL), crawl (site), map (sitemap) — bộ 4 endpoint cho agent | ta có scrape 1 trang; nên thêm `/v1/map` (sitemap) + `/v1/crawl` (site theo depth) |
| **OpenAI Deep Research / Gemini / Grok** | multi-hop hàng trăm nguồn, report dài có trích dẫn, pivot theo evidence tìm được | củng cố hướng IRCoT cho `research.py` |
| **Search API benchmark 2026** (Keiro blog: agentic search + FinanceBench) | Brave/Exa/Firecrawl/Tavily/Perplexity được chấm trên task agent; FinanceBench là chuẩn tài chính | thêm FinanceBench-style vào golden eval của ta (đang có judge FA) |

## 4. Đề xuất tính năng chi tiết (đã map vào kiến trúc hiện tại)

Ưu tiên P0 = làm ở v2.5 tiếp theo; P1 = v2.6; P2 = v3.x. Effort ước lượng (1 người, đã quen codebase).

### P0 — Nâng chất lượng retrieval/answer ngay (v2.5)

| # | Đề xuất | Chi tiết & nơi tích hợp | Bằng chứng | Effort | KPI |
|---|---|---|---|---|---|
| P0-1 | **RRF fusion chuẩn** ở `merger.py`: chuẩn hoá điểm từng adapter theo rank (1/(k+rank)), cộng có trọng số `EngineConfig.weight`, thay vì chỉ consensus đếm số engine | `app/retrieval/merger.py` (~80 LOC + test) | production RAG blueprints 2026; NirDiamant | 2–3d | hit@5 trên golden set +2–4pt |
| P0-2 | **Local cross-encoder reranker mặc định**: `bge-reranker-v2-m3` (Apache-2.0, đa ngữ, chạy CPU int8 qua ONNX Runtime — đã có `ONNXReranker` khung) làm tier giữa heuristic và API; `JinaAPIReranker`/NIM là tier trên khi có GPU/key | `app/retrieval/reranker.py` thêm class `BGERerankerONNX`; config `reranker_tier=heuristic\|bge\|api` | bge-reranker-v2-m3 MTEB/beir; so sánh với jina-reranker-v3 | 3–5d | context precision + rerank latency p95 < 300ms CPU |
| P0-3 | **Query expansion + RRF**: sinh 2–3 biến thể query (multi-query bằng LLM rẻ/free endpoint hiện có, hoặc rule-based: thêm/bớt stopword, dịch Anh↔Vi khi `lang` auto) rồi fan-out và fuse; bật khi `deep=true` hoặc query dài/mơ糊 | `pipeline.py` `_fanout` + `llm/gateway` gọi rẻ | HyDE, multi-query retriever | 3–4d | recall@10 +5–8pt trên golden đa hop |
| P0-4 | **Claim-level citation check**: sau khi synthesize, kiểm từng câu có `[n]` bằng NLI nhỏ (deberta-v3-xsmall-nli local) hoặc judge trong council hiện có; câu không được support → gắn cờ hoặc xoá `[n]` | `app/llm/citations.py` mở rộng + council | RAGTruth, HHEM | 4–6d | faithfulness judge ≥ 0.95, số câu bị gIVO flag giảm |
| P0-5 | **Follow-up suggestions + chat-with-answer**: sau mỗi answer, sinh 3 câu hỏi tiếp nối từ sources (LLM rẻ); UI nút gợi ý; thêm `session` để hỏi tiếp dùng context câu trả lời trước (query contextualization — đã có khái niệm hội thoại chưa?) | `answer.py` + `frontend` | Perplexity follow-ups, QueryContextualizer | 3–4d | tỉ lệ dùng follow-up, thời gian session |
| P0-6 | **Custom rerank rules theo API key (Goggles/Lenses-lite)**: `POST /v1/prefs/boosts` {domain boost/demote, keyword boost, lang prefer}; áp ở `merger.py` sau RRF; UI quản lý đơn giản | `app/retrieval/models` + `merger.py` + `api/v1` + UI | Brave Goggles, Kagi Lenses | 4–5d | user retention; A/B interleaving trên golden cá nhân hoá |
| P0-7 | **Agent-friendly search API**: thêm vào `/v1/search` response các field tuỳ chọn: `contents` (extract ngắn của page qua scraper hiện có), `highlights` (2–3 câu liên quan nhất trong page — extractive, không LLM), `answer` (instant answer ngắn từ top results, không LLM hoặc LLM rẻ) | `api/v1/search.py` + `extraction/scraper.py` + schemas | Exa contents/highlights, Tavily | 4–6d | agent use-case pass rate trên golden |
| P0-8 | **Eval harness v2**: thêm retrieval metrics (nDCG@10, recall@10, MRR) trên BEIR-style subset + MIRACL-vi subset nhỏ; thêm RAGAS context precision/recall + faithfulness vào eval hằng đêm; lưu kết quả vào DB để so sánh theo version | `scripts/eval*` + `tests/eval` + golden mở rộng 40→200 (đa ngữ, multi-hop, finance) | BEIR/MIRACL/TREC-RAG/RAGAS | 5–7d | CI nightly có bảng so sánh version |

### P1 — Deep research & knowledge (v2.6)

| # | Đề xuất | Chi tiết | Bằng chứng | Effort |
|---|---|---|---|---|
| P1-1 | **IRCoT-style research loop**: ở mỗi iteration, executor không chỉ search mà còn (a) reformulate query từ evidence đã thấy (entity-linking hop), (b) kiểm coverage các sub-question của planner trước khi dừng; thêm `max_iterations` đã có nhưng thêm coverage gate | `orchestration/research.py` executor + planner | IRCoT/Search-o1, Deep-RAG survey 2026, HotpotQA analysis | 5–7d |
| P1-2 | **Adaptive router**: phân loại độ phức tạp query (heuristic + LLM rẻ 1 lần) → route: simple (cache/L1, few engines) → standard → `deep`/research; tự động, không cần user bật Deep | `pipeline.py` router mới + config | Adaptive-RAG, Self-RAG | 4–6d |
| P1-3 | **Late chunking** cho doc dài (>8k tokens): embed whole doc (model dài context: jina-v3/v4 API hoặc bge-m3 local nếu fit) rồi gán vector theo chunk — áp ở L2/pgvector path | `extraction/chunker.py` + `llm/embeddings.py` + pgvector | Jina late chunking paper | 5–7d |
| P1-4 | **Hybrid dense+sparse trong chính chỉ mục của mình**: thêm cột tsvector (BM25) hoặc sparse vector của BGE-M3 vào pgvector; query side tính cả 2, fuse bằng RRF ở chunk level | `db/models.py` + alembic 0007 + `pipeline` scoring + BGE-M3 sparse output | BGE-M3, Marqo/Vespa hybrid | 6–8d |
| P1-5 | **Personal memory theo key (Mem0/Graphiti-lite)**: lưu past queries + feedback (👍/👎 từng result) vào pgvector per-key; dùng để rerank boost + query contextualization; có API xoá memory (privacy) | `db/models.py` (MemoryItem) + `api/v1/memory` + merger boost | Mem0, Graphiti, "Memory in the Age of AI Agents" survey | 6–8d |
| P1-6 | **Spaces/collections**: user tạo "Space" theo chủ đề (vd: "AI agents", "VN banking"), ghim sources, crawl định kỳ vào chỉ mục riêng của space (pgvector namespace theo space), search trong space bằng hybrid | `api/v1/spaces.py` + scraper job + pgvector filter | Perplexity Spaces | 8–12d |
| P1-7 | **Site map/crawl endpoints cho agent**: `POST /v1/map` (sitemap liệt kê URL), `POST /v1/crawl` (site theo depth, lưu vào chỉ mục tạm TTL) | `api/v1/scrape.py` mở rộng + robots/SSRF sẵn có | Tavily map/crawl | 5–7d |
| P1-8 | **Freshness decay cho kết quả thường**: điểm có decay theo `published_at` (exponential, half-life config) ngoài news; stale-source downrank tự động | `scoring.py` | freshness research; product time filters | 2–3d |

### P2 — Nâng cao & khác biệt (v3.x)

| # | Đề xuất | Ghi chú | Effort |
|---|---|---|---|
| P2-1 | **Multimodal retrieval cho PDF tài chính** (ColPali/jina-v4): render trang PDF BCTC thành ảnh, embed ảnh, hỏi "trang có biểu đồ doanh thu quý" → trả trang; cần GPU hoặc API | ColPali, jina-embeddings-v4, NeMo extraction NIM (bảng/biểu đồ) | 10–15d |
| P2-2 | **KG theo domain (LightRAG-style)** trên corpus đã crawl của 1 space:.extract entity/relation bằng LLM rẻ, lưu graph (pgvector + table quan hệ hoặc Neo4j tuỳ chọn), query mode hybrid + global | GraphRAG-Bench cho thấy chỉ lời ở global/summary — làm last | 10–15d |
| P2-3 | **Vietnamese-first NLP**: tokenization-aware index (underthesea) cho BM25 nội bộ, stopword nâng cao, bộ golden eval tiếng Việt (mở rộng từ EACL'26 Vietnamese QA corpora) | EACL 2026 findings | 4–6d |
| P2-4 | **Online eval + interleaving** cho thay đổi reranker/fusion: tự A/B nội bộ khi có traffic, log vào metrics, cảnh báo nếu metric online tụt | online eval correlation research | 5–7d |
| P2-5 | **Voice/agent UX**: voice search (đã có dictation), "đọc trả lời" (đã có TTS), podcast cá nhân hoá theo watchlist (mở rộng từ v2.3 D) | product trend Comet/NotebookLM | 5–8d |
| P2-6 | **Structured extraction từ search** (Exa-style JSON schema output): `response_format={json_schema}` để agent nhận object | Exa structured outputs | 3–4d |
| P2-7 | **Domain reputation / source tiers** (Kagi small-web, Brave independent): chấm điểm tên miền (official/primary vs aggregator vs UGC) từ đồ thị trích dẫn + whitelist built-in; hiển thị tier ở UI và boost nhẹ | Kagi/Brave | 4–6d |

## 5. Đề xuất của tôi: nên làm gì trước (thứ tự giá trị/chi phí)

1. **P0-1 RRF + P0-2 BGE reranker local + P0-3 query expansion** — bộ ba nâng chất lượng retrieval/answer ngay, toàn code nội bộ + 1 model open-weights, không cần hạ tầng mới. Đây là khác biệt lớn nhất so với "searxng + heuristic" hiện tại.
2. **P0-4 citation entailment + P0-8 eval v2** — củng cố "không ngụy tạo" (giá trị cốt lõi của repo) và có số đo để chứng minh mọi thay đổi sau không làm tụt.
3. **P0-6 custom boosts + P0-5 follow-ups + P0-7 agent API fields** — khác biệt sản phẩm rẻ, đúng hướng "search API cho agent" (Tavily/Exa đang dẫn).
4. Sau đó mới tới P1 (research loop IRCoT, adaptive router, late chunking, hybrid sparse, memory, spaces) rồi P2.

## 6. Những gì KHÔNG nên tự làm (giants-first)

- Không tự build web index quy mô Kagi/Brave (cần crawler fleet + pháp lý); tiếp tục SearXNG + keyless APIs.
- Không tự train LLM/embedding lớn; dùng open-weights (BGE-M3, jina, NeMo NIM) hoặc API free hiện có.
- Không tự build vector DB mới; pgvector đủ ở scale hiện tại, Vespa/Qdrant làvolevolevole future khi >100M chunk.
- Không tự viết crawler mới; crawl4ai + Firecrawl-style chỉ khi cần JS-heavy site.

## 7. Checklist nếu được duyệt triển khai

- [ ] docs/12 thêm phase 10 (v2.5: P0-1..P0-8) và phase 11 (v2.6: P1), phase 12 (v3: P2)
- [ ] Mỗi P0 có: schema/endpoint nếu có, test (unit + golden), cập nhật OpenAPI snapshot nếu đổi API
- [ ] Mỗi thay đổi retrieval/answer phải chạy eval harness v2 và so sánh với version trước (không merge nếu tụt)
- [ ] Audit 8 lens như các version trước; release theo `scripts/release.py`
