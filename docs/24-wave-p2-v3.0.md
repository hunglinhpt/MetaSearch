# docs/24 — Wave P2 (v3.0.0, từ docs/20 §4 P2)

Triển khai toàn bộ backlog P2 của docs/20 §4 trong một bản v3.0.0.Nguyên tắc giữ
nguyên: giants-first (pypdf, jsonschema — pinned dep, không tự viết parser/validator),
fail-open (mọi thành phần mới đều có thể tắt bằng env và không bao giờ làm fail request),
không ngụy tạo dữ liệu (structured output báo lỗi vào `structured_errors`, không bịa giá trị).

## P2-1 — PDF page-level retrieval (ColPali-lite, text side) (`extraction/pdf.py`, `retrieval/vision.py`, `extraction/scraper.py`, `orchestration/persist.py`)

- `pdf_pages_to_markdown()` dùng **pypdf** (pinned) để trích xuất text từng trang và phát markdown
  với heading `## p.N` → chunker cũ tự cắt chunk theo trang, citation có thể chỉ `page=N`.
  Bounded: `PDF_MAX_PAGES=60`, `max_chars=400k`; mọi lỗi pypdf → `""` (rơi về extract chain thường).
- Scraper: nếu body là `%PDF-` hoặc URL kết thúc `.pdf` → nhánh `extractor_used="pdf-pages"`;
  challenge page (anti-bot) vẫn đi luồng escalation cũ vì hook nằm sau `is_challenge_page`.
- Phần multimodal thật (render trang → ảnh → vector ColPali/jina-v4) cần GPU — theo đúng §6
  giants-first ta chỉ cung cấp **API path**: `VISION_EMBED_API_BASE_URL` + `VISION_EMBED_MODEL`
  (mặc định `jina-embeddings-v4`), `embed_texts_via_vision_api()` gọi endpoint `/embeddings` kiểu
  OpenAI và ghi vector vào cùng cột `chunks.embedding` → `POST /v1/hybrid` thành multimodal-capable
  không cần model local. Guard: vector sai `EMBEDDING_DIM` thì raise (caller fail-open), chỉ http(s).
- Khi L2 embedder tắt nhưng vision API có cấu hình, `_persist_documents` vẫn embed chunk qua vision API.

## P2-2 — Knowledge graph theo Space (LightRAG-lite) (`retrieval/graph.py`, `db/models.py` + alembic 0009, `api/v1/spaces.py`)

- Bảng mới `kg_nodes` / `kg_edges` (alembic 0009), scope theo `space_id` + `owner`, cascade theo space.
  Unique constraint `(space_id, label)` và `(space_id, source, target, relation)` → rebuild idempotent,
  bảng không phình vô hạn (phát hiện ở audit round 3).
- `POST /v1/spaces/{slug}/graph/build`: trích entity/relation từ tối đa `KG_MAX_DOCS_PER_BUILD=20`
  document đã crawl của space bằng một call LLM rẻ mỗi doc (`task="kg_extract"`, JSON sanitize bằng
  `parse_graph_json` — fold diacritics, dedupe, edge phải trỏ vào node tồn tại, cap `KG_MAX_NODES_PER_BUILD=200`).
- `POST /v1/spaces/{slug}/graph/query`: query mode entity-centric (LightRAG "local") — match token đã fold
  của câu hỏi vào label node, lấy subgraph 1-hop; `answer=true` thì tổng hợp trả lời bằng LLM rẻ chỉ trên
  context graph. GraphRAG "global" (community summary) cố ý bỏ — GraphRAG-Bench cho thấy chỉ lời ở câu
  summary toàn corpus, và tốn nhất (docs/20 §2).

## P2-3 — Vietnamese-first BM25 + golden eval VI (`retrieval/scoring.py`, `evals/golden_v3_vi.json`)

- `fold_diacritics()`: NFD strip Mn + `đ→d`; `tokenize(..., fold=True)` (mặc định) fold dấu nên
  "Đánh giá" và "danh gia" cùng token — người dùng gõ có dấu/không dấu đều match.
- Mở rộng `_EXTRA_VI_STOPWORDS` (dạng đã fold) trên nền stopword có sẵn.
- Golden eval mới `evals/golden_v3_vi.json` (25 câu VI, cặp có dấu/không dấu) chạy bằng
  `scripts/eval_v2.py --golden evals/golden_v3_vi.json` (cùng harness v2: hit@k/MRR/nDCG/claim support).

## P2-4 — Online interleaving (`retrieval/interleave.py`, pipeline, `cache.py`, `api/v1/ops_status.py`, `api/v1/memory.py`)

- Gán variant deterministic bằng sha256(`salt|owner|query`) — cùng (query, owner) luôn cùng một nhánh,
  không bao giờ thấy cả hai variant cho một query. `INTERLEAVE_RATIO=0.5` (tỉ lệ nhánh treatment "b").
- Treatment chạy với `INTERLEAVE_TREATMENT_RRF_WEIGHT=0.5` (control giữ `RRF_WEIGHT=0.3` mặc định) trong
  `merge()`; `interleave_sig` (`il:a`/`il:b`) được gộp vào `search_cache_key` nên hai nhánh không bao giờ
  share cache (đã kiểm chứng thứ tự: `_personalize` gán variant trước khi tính cache key).
- Response `/v1/search` thêm `interleave: {variant}`; feedback 👍/👎 có thể kèm `payload.variant` qua
  `POST /v1/memory`. `GET /v1/ops/interleave` tổng hợp up_rate theo variant (chỉ đếm feedback explicit,
  không metric click) — scan bounded 5000 row gần nhất (fix round 3: trước đó load toàn bộ bảng).

## P2-5 — Personalized podcast từ watchlist (`api/v1/podcast.py`, `tts/briefing.py`)

- `POST /v1/podcast/personal` (202, single-flight theo owner+ngày, cần VoiceStudio + `PODCAST_PERSONAL_ENABLED=true`),
  `GET /v1/podcast/personal/status`, `GET /v1/podcast/personal/latest` (audio/mpeg, `Cache-Control: private, no-store`).
- Nội dung lấy từ **watchlist của owner** (query → preset, freshness 2 ngày, cap bằng watchlist cap); hết
  watchlist thì fallback về `BRIEFING_PRESETS` global — không bao giờ bịa chủ đề. DB down → fallback an toàn.
- File `personal-<sha256(owner)[:8]>-<date>.mp3` (không lộ API key), chung retention với bản tin ngày
  (sweep đúng cả file personal + orphan `.tmp` — fix prefix_len round 1).

## P2-6 — Structured JSON output (Exa-style) (`llm/structured.py`, `api/v1/schemas.py`, `api/v1/search.py`)

- `SearchRequest.response_format = {type: "json_schema", json_schema, name}`; response có `structured`
  (object đã validate bằng **jsonschema** — pinned) + `structured_errors`. Post-cache, fail-open:
  LLM lỗi / JSON hỏng / không khớp schema → `structured: null` + lỗi trong `structured_errors`, không sửa số liệu.
- `ResponseFormat` định nghĩa TRƯỚC `SearchRequest` trong schemas.py — nếu để sau thì forward-ref không
  resolve khi subclass (`CouncilRequest` ở llm.py) rebuild và làm hỏng OpenAPI snapshot (đã dính ở round 1).

## P2-7 — Domain reputation tiers (`retrieval/tiers.py`, `scoring.py`, `models.py`, `cache.py`, `search.py`)

- Bảng tier: `official 0.97` (gov/regulator/exchange/central bank) > `primary 0.93` > `research 0.90` >
  `reference 0.88` > `aggregator 0.72` > `community 0.60` (forums/UGC) > `unknown 0.50` (mặc định, không đoán cao hơn).
- `domain_authority()`: bảng curated `_TRUSTED_DOMAINS` vẫn thắng nếu có; ngược lại dùng tier base.
  Match theo suffix có dấu chấm (".vnexpress.net") nên `evilvnexpress.net` không khớp.
- Mỗi result của `/v1/search` có thêm `domain_tier`, và cache serialize/deserialize round-trip field này.

## Test & gates

- `tests/test_v30_wave_p2.py` (43 test): fold/tokenize VI, tiers (exact/suffix/www/unknown + curated thắng),
  structured (valid/invalid/LLM lỗi/no JSON), interleave (bounds, deterministic, distribution, cache key,
  stats từ DB seed), PDF (pypdf build thật, `## p.N`, max_pages, garbage → ""), graph (sanitize, build+query
  trên sqlite, rebuild idempotent, vision dim guard), personal podcast (prefix/key/regex, retention, presets
  fallback, endpoint 503 khi thiếu VoiceStudio), ops endpoint smoke.
- `tests/test_models.py` EXPECTED_TABLES + `kg_nodes`/`kg_edges`; alembic 0009 chạy `upgrade head` trên sqlite thật;
  OpenAPI snapshot regen (6 endpoint mới + `response_format`/`structured`/`interleave`/`domain_tier`).
- Full pytest xanh; ruff check+format clean; bandit 0 issue; mypy `app/api app/core` clean;
  frontend typecheck + 49 vitest xanh.

## §8 Audit log (3 vòng tự audit, 8 lens: finance, technical/SA, AI/LLM, QA, auditor/security, data-engineer, dream-rsi, perf)

- **Vòng 1 (QA + SA + auditor):** phát hiện `extraction/pdf.py` bị hỏng string literal do heredoc
  (sau compileall phát hiện, viết lại file sạch); `ResponseFormat` định nghĩa sau `SearchRequest` làm vỡ
  OpenAPI snapshot (PydanticUserError ở `CouncilRequest`) → chuyển lên trước; `_prune_jobs` so sánh horizon
  với key personal sai → tách riêng; `cleanup_retention` parse sai prefix_len của file personal
  (`index("-", 8)` match dấu `-` đầu) → file personal cũ không bao giờ bị xoá → fix `len("personal-")+8+1`;
  tmp file personal không được dọn → regex tmp mở rộng.
- **Vòng 2 (perf + technical):** kiểm chứng thứ tự pipeline: variant gán ở `_personalize` (trước
  `search_cache_key`) nên hai nhánh interleave không share cache; scraper PDF hook nằm sau
  `is_challenge_page` nên không phá vỡ flow challenge; treatment RRF weight (0.5) khác control (0.3) nên A/B có ý nghĩa.
- **Vòng 3 (data-engineer + security + dream-rsi):** `kg_nodes/kg_edges` thiếu unique constraint → rebuild
  sinh duplicate vô hạn → thêm UniqueConstraint cả model lẫn migration 0009 + `on_conflict_do_nothing(index_elements=...)`
  + dedupe trong một build (có test rebuild idempotent); lưu ý `UniqueConstraint` nhận cột ở positional,
  `name=` là keyword (lỗi ConstraintColumnNotFoundError đã bắt được bằng test); `interleave_stats` load toàn bộ
  bảng → bounded 5000 row; vision embed thêm guard dim == EMBEDDING_DIM và chỉ nhận http(s) base URL.
- **Hai mục P3 đã xử lý ở v3.1.0:** (1) `POST /v1/spaces/{slug}/graph/build` chuyển sang background job
  single-flight (202 + `GET /v1/spaces/{slug}/graph/status`, admit tier `kg`, counts đọc từ bảng nên
  survives restart) — không còn chạy sync 20 LLM call trong request; (2) stopword check chuyển sang
  so trên token GỐC trước khi fold (xem `tokenize`), nên "cổ"→"co" không còn bị "có" nuốt — các từ extra
  va chạm với danh từ có nghĩa (sao/ngôi sao, do/đô, day/dây, ma, bac, nam) đã được loại khỏi danh sách.
- **Còn lại (P3, không chặn release):** voice search end-to-end và RAGAS-lite judge đã có trong backlog docs/23.
