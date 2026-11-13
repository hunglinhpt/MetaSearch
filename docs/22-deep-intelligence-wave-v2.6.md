# docs/22 — Deep Intelligence wave P1 (v2.6.0, từ docs/20 §4 P1)

Triển khai toàn bộ wave **P1** của [20-research-landscape-and-future-roadmap.md](20-research-landscape-and-future-roadmap.md):
8 mục, mỗi mục có config knob, test riêng và fail-open (không bao giờ làm fail
request vì một component optional). Tất cả đều per-key an toàn (cache key /
tenancy không leak giữa các key).

## P1-1 — IRCoT-style research loop + coverage gate (`orchestration/research.py`)

- Mỗi iteration, gap follow-up được **reformulate từ evidence đã thấy**:
  `_reformulate_query(question, gap, learnings)` → `"{question} — {gap}. Given
  what is already established ({facts...}), find:"` (entity-linking hop, theo
  IRCoT/Search-o1).
- **Coverage gate**: `_plan_coverage(plan_queries, learnings)` tính tỉ lệ
  sub-question của planner đã được learnings cover (lexical proxy, term overlap
  ≥ 0.25). Loop chỉ dừng khi `gaps` rỗng **và** `coverage ≥
  RESEARCH_COVERAGE_THRESHOLD` (0.6) — hoặc hết depth. Coverage được emit trong
  event `learnings` và `done`.
- Config: `RESEARCH_IRCOT_REFORMULATE=true`, `RESEARCH_COVERAGE_GATE_ENABLED=true`,
  `RESEARCH_COVERAGE_THRESHOLD=0.6`.

## P1-2 — Adaptive complexity router (`retrieval/router.py` + pipeline)

- `classify_complexity(query)` heuristic thuần (không tốn LLM): marker đa hop /
  so sánh / phân tích (VI+EN) hoặc query ≥ `ADAPTIVE_ROUTER_MIN_WORDS_DEEP` (14)
  từ → `deep`; ≤3 từ → `simple`; còn lại `standard`.
- `SearchPipeline._personalize` tự nâng `deep=true` khi classify = deep (kể cả khi
  caller không set) — Adaptive-RAG style. `deep` đã nằm trong cache key nên
  routed và explicit-deep share cache an toàn.
- Config: `ADAPTIVE_ROUTER_ENABLED=true`.

## P1-3 — Late chunking cho doc dài (`extraction/chunker.py`, `llm/embeddings.py`, `orchestration/persist.py`)

- `segment_long_document()` chia doc > `LATE_CHUNKING_DOC_MAX_CHARS` (24k) thành
  các segment window 6k char overlap 300 (cắt theo paragraph/sentence/space).
- `late_chunk_markdown()` chunk theo từng segment (position liên tục, chunk id
  re-key theo segment để overlap không trùng id) — dùng ở `/v1/crawl` và
  Spaces crawl.
- `EmbeddingClient.embed_many()` — batch embeddings (1 HTTP call cho nhiều
  segment). Ở persist time (`_persist_documents`), doc dài được embed theo
  segment và mỗi chunk **kế thừa vector của segment chứa nó** (xấp xỉ late
  chunking của Jina trên API backend không nhét được cả doc 100k token vào 1
  call); doc ngắn embed trực tiếp chunk. Bounded: ≤12 segment/≤32 chunk,
  timeout 10s, fail-open (row vẫn được ghi, chỉ không có vector).
- Embedder được wire vào persist ở startup (`persist.configure_embedder` trong
  lifespan) — chỉ hoạt động khi `EMBEDDING_L2 != off`.
- Config: `LATE_CHUNKING_ENABLED`, `LATE_CHUNKING_DOC_MAX_CHARS`,
  `LATE_CHUNKING_SEGMENT_CHARS`, `LATE_CHUNKING_SEGMENT_OVERLAP_CHARS`.

## P1-4 — Hybrid dense+sparse chunk retrieval (`db/models.py` + alembic 0008 + `retrieval/hybrid.py` + `POST /v1/hybrid`)

- Bảng `chunks` thêm cột `tsv` (TSVECTOR thật trên Postgres + GIN index + backfill
  `to_tsvector('simple', content)`; SQLite lưu raw text — `TSVType` ở `db/base.py`).
  Persist ghi `tsv` theo dialect (`func.to_tsvector` trên PG).
- `hybrid_chunk_search()`: candidate set bounded (1000 chunk mới nhất, pre-filter
  theo URL/domain ở SQL), sparse = BM25-lite trong Python, dense = cosine với
  `chunks.embedding` (khi có), fuse bằng **RRF ở chunk level** (cùng hằng k=60 với
  merger). Không có dense vector → sparse-only, không bao giờ lỗi.
- Endpoint `POST /v1/hybrid {query, k, urls?, domains?, space?, dense?}` — hỗ trợ
  search trong Space (domain scope của space). `dense_used` trong response cho
  biết có dùng được dense side không.
- Config: `HYBRID_SEARCH_ENABLED=true`, `HYBRID_RRF_WEIGHT` (mặc định 0.5 —
  hiện tại dùng để réserve; fusion thuần RRF, weight giữ cho API tương lai).

## P1-5 — Per-key memory, Mem0-lite (`db/models.py` + alembic 0008 + `retrieval/memory.py` + `api/v1/memory.py`)

- Bảng `memory_items` (owner = api key): `kind=query` (mỗi search của key được
  ghi fire-and-forget qua **bounded persist queue**, không phải 1 task/request)
  và `kind=feedback` (👍/👎 theo URL, weight ±1).
- `load_memory_boosts()` aggregate feedback theo domain thành
  `BoostRule(kind="domain")` (clamp ±5, nhân `MEMORY_FEEDBACK_BOOST=0.15`), gộp
  với `key_boosts` trong `_personalize` → vào `boosts_sig` → không leak giữa
  key. Bounded 0.3s + TTL cache 30s, tự invalidate khi có feedback mới hoặc xoá.
- API: `GET/POST /v1/memory`, `DELETE /v1/memory` (xoá TOÀN BỘ memory của key —
  privacy), `DELETE /v1/memory/{id}`. Cap `MEMORY_MAX_PER_KEY=500` (507 khi đầy).
  Research sub-query không ghi memory (`record_memory=False`).
- Config: `MEMORY_ENABLED=true`, `MEMORY_MAX_PER_KEY`, `MEMORY_FEEDBACK_BOOST`.

## P1-6 — Spaces (`db/models.py` + alembic 0008 + `retrieval/spaces.py` + `api/v1/spaces.py`)

- Bảng `spaces` (per-key): name/slug (auto, unique per key), domains,
  pinned_urls, query_terms, `restrict`.
- CRUD `/v1/spaces` (+ `PATCH/DELETE /{slug}`, `POST /v1/spaces/{slug}/crawl` —
  map domain root + scrape pinned URL vào index, cập nhật `last_crawl_at`).
  Cap `SPACES_MAX_PER_KEY=20`.
- Search trong space: `POST /v1/search` và `/v1/answer` nhận thêm `space=<slug>`.
  `restrict` + domains → post-cache include-domain filter (không đụng cache
  chung); pinned URLs → `BoostRule(kind="url")` mới (exact canonical match, nhân
  hệ số clamp ±2x) qua `boosts_sig`. Space không tồn tại → 404.
- Config: `SPACES_ENABLED=true`, `SPACES_MAX_PER_KEY`.

## P1-7 — `/v1/map` + `/v1/crawl` cho agent (`extraction/sitemap.py` + `api/v1/site.py`)

- `POST /v1/map {url, max_urls}`: sitemap.xml trước (đệ quy sitemapindex, tối đa
  8 sitemap), fallback/bổ sung link same-host trong HTML. Fetch qua
  `Scraper._guarded_fetch` (SSRF resolve+check, IP pinning, robots mỗi hop),
  XML parse bằng **defusedxml** (đã pin trong pyproject), chỉ same-host.
- `POST /v1/crawl {url, depth≤3, max_pages≤100}`: BFS same-host qua `scrape_many`
  (robots/SSRF/pinning/extract chain sẵn có), link mức sau lấy từ markdown đã
  extract; trang được persist vào index (chunk bằng late chunking) → `indexed=true`.
  `crawl_site()` dùng chung cho `/v1/spaces/{slug}/crawl` (không lồng 2 slot
  admission — tier limit=1 sẽ tự deadlock).
- Config: `MAP_MAX_URLS=200`, `CRAWL_MAX_PAGES=50`, `CRAWL_MAX_DEPTH=2`.

## P1-8 — Freshness decay half-life (`retrieval/scoring.py` + merger + pipeline)

- `freshness()` nhận `half_life_days`: `0.5 ** (age/half_life)`; không truyền thì
  giữ legacy `exp(-age/30)` (đúng bằng half-life `30·ln2 ≈ 20.794`).
- News giữ nguyên hành vi cũ (default 20.794); non-news half-life mặc định
  **45 ngày** — tài liệu cũ bị downrank tự động nhưng nhẹ hơn news nhiều.
  `data_kind="computed"` vẫn luôn 0 (không bao giờ coi dữ liệu tính toán là "fresh").
- Config: `FRESHNESS_HALF_LIFE_DAYS_NEWS=20.794`, `FRESHNESS_HALF_LIFE_DAYS_OTHER=45.0`.

## Test & gates

- `tests/test_v26_wave_p1.py` (38 test): freshness half-life, router, IRCoT helpers
  + full research loop (coverage trong events), memory record/boosts/endpoint CRUD,
  sitemap parse + map_site (fake scraper), Spaces CRUD + restrict filter + url boost,
  late chunking segments + embed_many (MockTransport), BM25/RRF/hybrid trên SQLite.
- Cập nhật: `test_models` (+memory_items, spaces), `test_research` (coverage gate
  → 3 iteration), OpenAPI snapshot regen (thêm `space` vào SearchRequest +
  6 endpoint mới).
- Full pytest xanh, ruff/bandit/mypy sạch; frontend không đổi (wave API-only).

## §8 Audit log (3 vòng tự audit, 8 lens)

1. **Vòng 1 (SA/QA)**: `cast` từ `sqlalchemy` shadow `typing.cast` ở memory.py →
   table ensure fail im lặng; memory record dùng raw `create_task` → vi phạm bounded
   queue (test w3_b phát hiện) → chuyển qua `persist.spawn_task`; crawl lồng
   `admit("crawl")` có thể tự deadlock → tách `crawl_site()` không admission.
2. **Vòng 2 (security/auditor/data)**: sitemap XML untrusted → defusedxml (B405/B314);
   domain suffix match thiếu dấu chấm (`evilexample.com` khớp `example.com`) ở hybrid
   filter → fix `== d or endswith("."+d)`; migration 0008 chạy được cả SQLite (GIN
   index + backfill chỉ trên Postgres); research sub-query không ghi memory
   (`record_memory=False`).
3. **Vòng 3 (perf/AI/finance)**: `_personalize` thêm tối đa 3 DB read nhưng đều
   bounded 0.3s + TTL 30s; memory feedback invalidate cache ngay khi ghi/xoá; freshness
   news giữ đúng legacy (20.794 ≈ 30·ln2) để không đổi điểm số news; `computed`
   data luôn fresh=0; chunk embedding bounded (12 segment/32 chunk, timeout 10s,
   fail-open).
