# docs/21 — Retrieval & Answer Quality wave (v2.5.0, từ docs/20 P0)

Triển khai wave P0 của [20-research-landscape-and-future-roadmap.md](20-research-landscape-and-future-roadmap.md):
nâng chất lượng retrieval/answer ngay bằng code nội bộ + 1 model open-weights,
không cần hạ tầng mới. Mọi thay đổi đều có test; mọi thứ fail-open (không bao giờ
làm fail request vì một optional component).

## P0-1 — RRF fusion trong `merger.py`

- `merge()` nhận thêm `rrf_weight` (mặc định `RRF_WEIGHT=0.3`) và `boosts`.
- Mỗi nhóm URL cộng dồn `adapter_weight / (RRF_K + rank + 1)` với `RRF_K=60` qua MỌI
  raw occurrence (kể cả các biến thể query mở rộng) — weight của engine lấy từ registry
  nên engine được operator điều chỉnh cũng counted trong fusion.
- Điểm cuối: `score = (1-rrf_weight)*signals + rrf_weight*rrf_norm` (rrf_norm = rrf/max_rrf).
- `ScoredResult` thêm field `rrf_score`, `boosts_applied`.
- Cache key search bump `sr:v2:` → `sr:v3:` (thêm `boosts_sig`, `expansion_sig`).

## P0-2 — BGE reranker local (`RERANKER=bge`)

- `BGEReranker(ONNXReranker)` trong `reranker.py`: model `BAAI/bge-reranker-v2-m3`
  (Apache-2.0, đa ngữ có vi), prefer `model_int8.onnx` nếu có.
- Resolution: `BGE_RERANKER_DIR` → nếu trống tự `snapshot_download` từ
  `BGE_RERANKER_HF_REPO` (pin `revision="main"`) về `MODEL_CACHE_DIR`
  (mặc định `~/.cache/metasearch/models`). Thiếu dep/network → `RerankerUnavailable`
  ở lần dùng đầu và pipeline giữ nguyên heuristic order (đúng quy ước graceful).

## P0-3 — Multi-query expansion + RRF

- `app/retrieval/query_expansion.py`: một call `gateway.complete` rẻ (≤160 tokens,
  task="expand") sinh tối đa `QUERY_EXPANSION_MAX_VARIANTS` biến thể; chỉ chạy khi
  `deep=True` hoặc query ≥ `QUERY_EXPANSION_MIN_WORDS` từ; provider `none`/lỗi → [].
- `SearchPipeline._personalize` gắn variants vào params; mỗi variant fan-out RIÊNG
  tới các engine `general` (`general_only=True`) để bound chi phí; RRF trong merge
  fuse tất cả raw. `expansion_sig` vào cache key.
- `SearchPipeline` nhận thêm `gateway` (deps đã truyền `get_gateway()`).

## P0-4 — Claim-level citation check

- `app/llm/citations.py` thêm `split_sentences`, `lexical_entailment` (tỉ lệ content
  word của câu có trong chunk, proxy không cần model), `check_claims`: với mỗi câu
  có `[n]`, nếu max entailment trên các chunk được trích dẫn < `CLAIM_ENTAILMENT_THRESHOLD`
  (0.34) thì marker bị flag.
- Kết quả được BÁO CÁO (`unsupported_markers` trong event `citations` + field
  `AnswerResponse.unsupported_markers`), không tự ý xoá text — "không ngụy tạo" vẫn giữ.
- `CITATION_NLI_ENABLED` + `CITATION_NLI_DIR` để dành cho model NLI ONNX (chưa bật mặc định).

## P0-5 — Follow-up suggestions

- Sau mỗi answer (cả council và thường), một call rẻ (≤200 tokens, task="followups")
  sinh ≤ `FOLLOWUPS_MAX` câu hỏi tiếp theo — sự kiện SSE mới `follow_ups`, có trong
  `done` payload, lưu vào answer cache và replay khi cache hit; `AnswerResponse.follow_ups`.
- Guard: bỏ qua khi provider `none` hoặc budget còn < 25% (không đốt phần đuôi của
  request gấp). UI: chip "Hỏi tiếp" dưới câu trả lời.

## P0-6 — Custom boosts theo API key (Goggles-lite)

- Bảng `key_boosts` (alembic 0007) + model `KeyBoost`; CRUD `/v1/prefs/boosts`
  (GET/POST/DELETE, cap `BOOSTS_MAX_PER_KEY=50`, upsert theo (kind,target), 404 không
  leak owner, DB down → list trả rỗng `storage="memory"`, write 503).
- Rule `domain` (suffix match, nhân hệ số, clamp ±2x) và `keyword` (cộng ±0.2×weight);
  áp trong `merge()` SAU blend, chỉ cho key sở hữu (`current_api_key_id`),
  `boosts_sig` vào cache key nên không leak giữa các key.
- `load_boosts` bounded 0.3s (DB chậm không vào critical path), cache 30s/owner.

## P0-7 — Agent-friendly extras trên `/v1/search`

- `SearchRequest.extras=["contents","highlights","answer"]` (đặt tên `extras` vì
  `ContextRequest` đã dùng `include`).
- `contents`: scrape top ≤ `SEARCH_EXTRAS_MAX_URLS` URL bằng scraper có sẵn (SSRF/robots
  unchanged), cắt `SEARCH_EXTRAS_MAX_CHARS`; `highlights`: top câu extractive theo
  keyword overlap (không LLM); `answer`: một câu trả lời ngắn ở response-level, chỉ khi
  có LLM. Tính SAU cache (kết quả base được cache, extras luôn fresh). Mọi lỗi → bỏ field.
- UI: nút 📦 Extras ở tab Raw (instant answer + highlights trong kết quả).

## P0-8 — Eval harness v2

- `app/eval_metrics.py` (pure): hit@k, recall@k, MRR, nDCG@k, context precision/recall
  (RAGAS-style), `claim_support_rate`, `aggregate`.
- `evals/golden_v2.json`: 29 query EN+VI với `relevant` URL substrings + thresholds.
- `scripts/eval_v2.py`: chạy live qua `/v1/search` (+ `--answers` qua `/v1/answer`),
  ghi report `evals/reports/eval_v2_<ts>.json` và in delta so với report trước —
  một version không thể lùi retrieval một cách im lặng.

## Test & gates

- `tests/test_v25_retrieval_quality.py` (37 test): RRF/boosts/expansion/reranker factory/
  claim check/extras/metrics/pipeline personalize/prefs CRUD.
- Cập nhật: `test_cache_layer` (sr:v3), `test_models` (+key_boosts), 2 test đếm LLM call
  (follow-ups +1 call cho answer hoàn tất), OpenAPI snapshot regen.
- Full pytest xanh, ruff/bandit/mypy sạch, frontend 49 vitest + tsc + next build xanh.

## §8 Audit log (3 vòng tự audit, 8 lens)

1. Vòng 1 (SA/QA): phát hiện `load_boosts` có thể block vào DB → bọc `wait_for(0.3s)`;
   `include` trùng tên với `ContextRequest.include` → đổi thành `extras`; worker retention
   marker fix (từ round 13 còn sót: `last_run_path=None` chạy purge mỗi 120s → marker file thật).
2. Vòng 2 (perf/finance/data): expansion chỉ fan-out general engine; follow-ups skip khi
   budget còn <25%; cache key thêm boosts/expansion sig (không leak giữa key); claim check
   chỉ báo cáo, không sửa text; `sr:v2→v3` để vô hiệu hoá cache cũ safely.
3. Vòng 3 (security/auditor/AI): sha1 trong cache key thêm `usedforsecurity=False`;
   `snapshot_download` pin `revision="main"` + nosec B615; prefs endpoint không leak owner
   (404 chung), cap per key, validate target/weight; extras không cache (freshness).
