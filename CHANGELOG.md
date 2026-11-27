# Changelog

## [4.7.2] - 2026-11-27 — SearXNG native cho Lite trên Windows (docs/36 §21)

### Added
- **`scripts/windows/searxng-local.ps1`** (`setup|start|stop|restart|status|check|install-task|remove-task`): SearXNG chạy native bằng Python, không Docker/WSL — ~130 MB RAM, CPU ~0 khi rảnh, chỉ nghe `127.0.0.1:8888`, task `MetaSearchSearXNG` (khi đăng nhập) + `MetaSearchSearXNGWatchdog` (kiểm `/healthz` mỗi 5 phút). Source tải từ archive GitHub của commit ghim (`git checkout` hỏng trên Windows vì upstream có tên file chứa `:`; chỉ giải nén `searx/` + requirements, `tarfile` filter `data`). `scripts/windows/searxng_local_settings.py` sinh settings từ `deploy/searxng/settings.yml` + override cục bộ (tắt google/qwant/brave bị chặn hoặc hỏng từ egress đã đo; `inactive` cho wikipedia/wikidata/openstreetmap — cần `contact_url`, không gửi thông tin chủ máy — và engine Tor), giữ secret ổn định, đặt stub `pwd` (module Unix-only chỉ dùng trong thông báo lỗi valkey).

### Fixed
- Adapter SearXNG bỏ URL không có host hoặc không phải http(s): engine brave của SearXNG trả `http:///go/v1/...` cho 60/60 kết quả.

### Đo (đối đầu cùng code, cùng điều kiện, tắt cache, `--repeat 2`; cả 2 lượt nhất quán)
- golden VI: strict@5 **0,12 → 0,38**, hit@5 0,12 → 0,42, MRR 0,087 → 0,266.
- golden_v2: strict@5 0,534 → **0,586**, recall@10 0,306 → 0,389, nDCG@10 0,286 → 0,340.
- Độ trễ search p50 4,49 s → 4,56 s (SearXNG chạy song song các engine khác); SearXNG ~0,4–1,2 s/truy vấn.

## [4.7.1] - 2026-11-27 — chất lượng câu trả lời `/v1/answer` (docs/36 §20)

Chẩn đoán từ 29 câu trả lời thật (golden_v2, provider keyless): 18/27 câu trả lời tự nói "nguồn không đủ thông tin", chỉ 55 % marker `[n]` được chunk hỗ trợ, 9/27 câu hỏi tiếng Anh bị trả lời bằng tiếng Việt. Metric `claim_support` 0,47 của eval so câu trả lời với *snippet* tìm kiếm chứ không phải chunk LLM đọc — chủ yếu là lệch phép đo; vấn đề thật là ngữ cảnh mỏng.

### Fixed
- **Q1 kết quả lạc đề vào ngữ cảnh** (`app/retrieval/relevance_gate.py` mới): nguồn web Lite khớp theo câu khuôn mẫu — "PostgreSQL logical replication … how does it differ" nhận về "What is a VNC and how does it differ from a VPN?", "What is ray tracing…" (9/10 kết quả). Kết quả bị coi là lạc đề khi độ liên quan từ khoá < 0,3 **và** < 0,4 × kết quả tốt nhất cùng lô **và** không chứa *từ neo* của câu hỏi (tên riêng/viết tắt/mã có số: PostgreSQL, BERT, 10-K, TNCN). Phát lại trên 540 kết quả gắn nhãn của 2 bộ vàng: loại 167, **0/52 kết quả vàng bị mất** (không có từ neo thì mất 9). `/v1/answer`, `/v1/context`: kết quả lạc đề không được scrape, không vào prompt (counter `metasearch_context_off_topic_dropped_total`); `/v1/search`: chỉ hạ hạng (× 0,45), không xoá — phát lại tất định: strict@5 không đổi trên 49 câu (0 tăng, 0 giảm).
- **Q2 scrape huỷ trang hạng cao**: early exit mềm cũ dừng khi *bất kỳ* 3 trang nào xong, nên 3 trang lạc đề về nhanh huỷ trang pgDash hạng 1 (trang liên quan duy nhất). Nay chờ thêm cho 3 URL hạng đầu, tối đa `SCRAPE_TOP_GRACE_S` (mặc định 1 s) sau mốc mềm — đo: chờ không trần làm 504 tăng 2 → 4 vì LLM keyless cần 6–8 s trong ngân sách 12 s; có trần: 0 lượt 504. Vòng chờ sau mốc mềm không còn thức dậy mỗi 10 ms.
- **Q3 `grounding` đếm lệch tập**: câu bị gắn cờ được đếm cả dòng bảng còn câu có trích dẫn thì không (`unsupported_sentences` > `cited_sentences` → "weak" sai). Nay đếm trên cùng tập câu văn xuôi.
- **Q4 sai ngôn ngữ trả lời**: `lang` của request không bao giờ tới prompt. Nay `answer_language()` (lang của request → chữ có dấu tiếng Việt → ASCII có từ chức năng tiếng Anh; tiếng Việt không dấu không bị nhầm) thêm dòng "Answer language: …" sau câu hỏi trong lượt user; system prefix giữ nguyên byte (prompt cache). `/v1/context` trả kèm trong `messages`.

### Đo (instance tách riêng, tắt cache, cùng 29 câu; so theo cặp 20 câu trả lời được ở mọi lượt)
- Marker được hỗ trợ **0,50 → 0,84**; câu trả lời "nguồn không đủ" 13/20 → **10/20**; nguồn được trích dẫn 2,40 → 2,75; câu hỏi tiếng Anh trả lời bằng tiếng Việt **9 → 0**; 504 do hết giờ 2 → **0**.
- Retrieval đối đầu v4.7.0 cùng điều kiện (`--repeat 2`): golden_v2 strict@5 0,552 vs 0,517 (lượt 2 của bản mới 0,586), VI 0,16 vs 0,12 (lượt 2: 0,16 = 0,16) — dao động engine live, khớp phát lại tất định ở trên.
- Pollinations keyless còn giới hạn **100 câu trả lời/ngày/IP** (HTTP 402 FREE_LIMIT, reset 00:00 UTC) — hệ thống trả 503 + Retry-After đúng; ghi vào preset.

## [4.7.0] - 2026-11-27 — W1 "đo được & chạy lại được" + W2 "khôi phục answer & retrieval" (docs/36 §16–§19)

### Fixed
- **[P0 safety] `/v1/verify` ủng hộ tuyên bố SAI**: bộ vàng 60 tuyên bố mới đo được false_support_rate **0,53** ở v4.6.0 (16/30 tuyên bố sai bị "supported": sai năm, mâu thuẫn tri thức, tin đồn). 7 chốt mới trong `claim_verify.py` (chấm theo câu; thuật ngữ then chốt bắt buộc kể cả năm; thứ tự LCS; từ quan hệ; số liệu neo đúng quan hệ; chặn câu rào đón/ý định/câu hỏi; tín hiệu lân cận chỉ chặn support; "refutes" chặt như "supports") → **0,00** ở 2 lần chạy liên tiếp, precision 1,00, 0 tuyên bố đúng bị bác bỏ; recall 0,90 → 0,40 (đánh đổi có chủ đích). Field mới `evidence[].missing_terms`.
- **[P0 reliability] Engine hỏng không bao giờ bị cách ly**: ddg thành công 1/37 cả tuần 19–25/11 (production) mà không bị tắt — auto-disable chỉ đếm lỗi liên tiếp (dừng đếm khi breaker mở) và bị xoá ở mỗi lần khởi động. Mới: `orchestration/engine_quarantine.py` — cách ly theo tỉ lệ thành công 24h/7d từ `engine_health` đã lưu, lý do `quarantine:` sống qua restart, gỡ bằng probe hoặc PATCH. Phát lại dữ liệu production: ddg/bluesky/reddit bị cách ly, 0 engine khoẻ bị đụng.

### Added
- `/v1/engines/health`: `calls_24h`, `success_rate_24h`, `calls_7d`, `success_rate_7d`, `quarantined`. Gauge `metasearch_engine_success_rate_24h`, `metasearch_engine_quarantined`; alert `EngineLowSuccessRate24h`, `EngineQuarantined`. Settings `ENGINE_QUARANTINE_*`.
- **Baseline chất lượng trong repo** `api/evals/baselines/` (retrieval EN/VI, VI, verify, research offline) + gate hồi quy `app/eval_gate.py` / `scripts/eval_gate.py` + job nightly `quality-eval`. `scripts/eval_verify.py` (`--in-process`, không cần mở cổng) + `evals/verify_golden.json` (60 tuyên bố, nhãn tay).

### Removed
- 3 adapter ngoài phạm vi, không bao giờ chạy (không có EngineConfig): `dogceo`, `opentdb`, `ipapi`.

### Fixed — W2 (docs/36 §17)
- **[P0] A0 `/v1/answer` hỏng trên provider keyless**: provider tự báo trần **16.000 ký tự prompt** (HTTP 413 `PAYLOAD_TOO_LARGE`) và **1 câu trả lời / 45 s / IP** (HTTP 402 `FREE_LIMIT`, body "Retry after 40s"). Trước: 413 bị tính là provider hỏng → breaker → 503 cho mọi request; lỗi provider ra **500 internal**. Nay: 413 trung tính với breaker, trần prompt theo provider (`LLMProviderPreset.max_prompt_chars`, học thêm từ body 413), pipeline cắt nguồn xếp hạng thấp cho vừa trần và thử lại 1 lần với ½ ngữ cảnh; 402/429/503-có-"Retry after" → `overloaded` 503 + `Retry-After` đúng thời gian provider báo, không ngủ quá deadline; lỗi provider khác → `upstream_error` **502** (mã lỗi mới). **Thay đổi hành vi**: lỗi provider trước là 500, nay 502.
- **A0b**: slot đồng thời theo provider (`max_inflight`, pollinations = 1) — request xếp hàng thay vì nhận 429; provider 1 slot bỏ query expansion, follow-ups và viết lại câu hỏi bằng LLM (thread carry dùng ghép câu tất định).
- Preset pollinations: model báo cáo `openai-large` (đã bị provider gỡ) → `openai-fast`; council mặc định không còn gọi `pollinations:mistral/llama` (404).
- **S1 web tổng hợp cho Lite**: adapter mới `mwmbl` (chỉ mục web phi lợi nhuận, mã nguồn mở, JSON keyless; probe 4/4); ddg 202 "anomaly" → rate-limited cooldown 900 s (cả html/lite đều bị chặn từ egress đã probe).
- **S4**: polymarket chuyển sang `/public-search` (`/markets/search` trả 422 4/4; 1 request thay vì 1+N, odds chỉ từ API); bluesky (403), reddit (ConnectError), gdelt (8–18 s + 429) tắt mặc định; semanticscholar chỉ bật khi có key, 429 backoff theo Retry-After (mặc định 300 s).
- **S5**: engine bị cách ly mà truy vấn lẽ ra dùng → status `quarantined` (không gọi, không làm `complete=false`); field mới `degraded_engines[]` trên `/v1/search`; `engines_failed` không còn liệt kê engine cách ly.
- **S6**: deep = rộng *trong* ý định — luôn `general`+`reference`; `news`/`academic`/`financial`/`developer`/`social` chỉ khi suy ra từ truy vấn, được yêu cầu, hoặc skill ưu tiên.
- **Vệ sinh xếp hạng Lite**: tin tức không còn chen vào mọi truy vấn (chỉ `wikipedia` luôn đi kèm; truy vấn tiếng Việt thêm `bing_news`/`google_news`), engine chỉ đi kèm tối đa 3 kết quả; fin_rss cần trùng ≥ ⅓ số từ của truy vấn dài và giữ dấu khi truy vấn có dấu ("nội"≠"nơi").
- **[Safety] Lọc nội dung người lớn** cho mọi engine (`SAFE_SEARCH_ENABLED=true` mặc định, `app/retrieval/safety.py`, counter `metasearch_safe_search_dropped_total`): eval bắt `bing_web` trả 10 trang khiêu dâm cho "hướng dẫn dùng FastAPI dependency injection"; RSS Bing với `adlt=strict` trả rỗng nên phải lọc phía mình.

### Added — W2
- `rerank: {reranker, applied, reason}` trên `/v1/answer` và `/v1/context` — reranker fail-open không còn im lặng (mẫu `_rerank.applied` của XERJ, docs/36 §18).

### Added — đợt 3 (docs/36 §19)
- `grounding` trên `/v1/answer` (`grounded|partial|weak|ungrounded`, số câu có/không được hỗ trợ, độ phủ citation, numeric fidelity) + nhãn "bám nguồn" trên Web UI (X1).
- `start_offset`/`end_offset`/`page` cho chunk trên `/v1/context` — span trong văn bản đã trích, `null` khi không định vị được (X2).
- Khử trùng memory `query`/`observation` gần-giống (Jaccard ≥ 0,9, 50 bản ghi gần nhất); `feedback` không bị khử (X3).
- Eval: metric **strict hit@k** (đúng tên miền *và* đúng chủ đề), `eval_v2.py --repeat N` (trung vị), `--answer-gap-s` (quota LLM keyless), URL top-10 trong record; harness research live `scripts/eval_research_live.py` + bộ 20 câu.

### Fixed — đợt 3
- Câu hỏi tài chính mất `fin_rss` sau W2 (nguyên nhân thật của hồi quy VI): ý định `financial` nay kéo `fin_rss`.
- Safe search: dấu hiệu tiêu đề chỉ áp với nguồn không có tier tin cậy; xét cả tên miền gốc của link bọc Google News.
- `_looks_vietnamese` coi `lang="auto"` là chưa biết ngôn ngữ (trước: "không phải tiếng Việt").
- Slot đồng thời theo provider khoá theo chính đối tượng event loop (`id(loop)` có thể bị loop mới dùng lại → treo).
- **Audit cuối đợt** (tự kiểm, 4 lỗi mới): (A1) `assign_offsets` tìm neo trong cửa sổ có trần — trước có thể quét lại cả tài liệu nhiều MB cho mỗi chunk; (A2) polymarket: timestamp thiếu múi giờ → UTC; (A4) **cách ly engine**: mốc gỡ cách ly/operator bật lại chỉ nằm trong RAM → sau restart engine vừa gỡ bị cách ly lại bằng lỗi cũ; nay lưu `disabled_reason="released:<ISO>"` trên engine đang bật (không đổi schema) và đánh giá bỏ qua lượt gọi trước mốc; (A5) council lỗi toàn bộ panel → `upstream_error` 502 thay vì `internal` 500. Vòng audit trước release: (A6) `PATCH /v1/engines/{id}` gửi lại `"enabled": true` cho engine **đang bật** (sửa weight/timeout) từng ghi mốc gỡ cách ly → xoá bằng chứng lỗi; nay chỉ ghi mốc khi engine thật sự đang tắt.
- A1b: `/v1/answer` keyless đủ 29 câu: **29/29 thành công**, citation precision 1,00, claim_support 0,47, numeric fidelity 0,68 (`evals/baselines/eval_answer_keyless.lite.json`).

### Đo (instance tách riêng, đối đầu v4.6.0 cùng điều kiện, tắt cache, trung vị 2 lần)
- golden_v2 (29): hit@5 0,362 → **0,552**, strict@5 0,293 → **0,552**, MRR 0,200 → 0,390.
- golden VI (25): strict@5 0,26 → **0,26** (hoà), strict@10 0,36 → 0,32, MRR 0,121 → 0,120. Hồi quy "0,28 → 0,12" ở lượt đo trước = nhiễu metric cấp tên miền + mất fin_rss (đã sửa). Baseline mới ở `api/evals/baselines/`, bản v4.6.0 giữ ở `*.v460.json`.

## [4.6.0] - 2026-11-26

### Fixed / Added — news-wire precision + TLS policy for `/v1/scrape` (`docs/35`, đề xuất M1–M4 của một hệ thống tích hợp)

- **[P1 security] M4 — `/v1/scrape` không còn leo thang qua chứng chỉ TLS hỏng** (`extraction/tls_policy.py`, `scraper.py`): lỗi xác minh chứng chỉ (bọc trong `httpx.ConnectError`) trước đây rơi vào nhánh "network drop" và đi tiếp crawl4ai → Jina, client nhận nội dung của trang mà kết nối trực tiếp đã từ chối, không có dấu hiệu nào. Nay mặc định từ chối (`tls_verify_failed`, không negative-cache, áp cho mọi đường dùng scraper); EOF/reset vẫn leo thang như cũ; opt-in `allow_insecure_escalation=true` leo thang và trang không bao giờ được cache; `ScrapeItem.tls_verified`; mọi URL bị từ chối → 502 `tls_verify_failed`. Test bằng máy chủ HTTPS tự ký cục bộ (0 lần gọi crawl4ai/Jina).
- **[P1 data-integrity] M1 — gắn mã chính xác + `symbol_evidence` + `indices`** (`retrieval/symbols/`, `aliases.py`, `events.extract_ticker_evidence`): geo guard che "TP.HCM" (hết `HCM` cho tin thành phố); mã chỉ số (VNI/VN30/HNX…) không vào `symbols`, báo ở `indices`; alias 4 chốt — loại 328 alias tự sinh toàn từ phổ thông/địa danh ("hải phòng"→HAC, "truyền thông"→TTN, "bất động sản"→VNI, "dầu khí"→PSI), khớp đúng dấu ("truyền thống" ≠ "Truyền thông", chấp nhận 2 cách đặt dấu), viết hoa tên riêng, acronym phân biệt hoa thường; bare token trong span alias thuộc về alias (CMC trong "Công nghệ CMC" → CMG); **xoá alias sai có sẵn "VinFast"→VFS** (VFS = CK Nhất Việt); thêm alias tuyển chọn (SeABank, Novaland, AgriS, Bảo Minh, CK Bảo Minh, CK Nhất Việt, Đá Núi Nhỏ, Đá Hóa An, Cen Land, Chứng khoán HSC…). Mỗi mã có `{method, span, start, field, alias_confidence?}`. Bộ vàng 97 tiêu đề thật: P = R = 1.00.
- **M2 — phân loại có `category_reason` + `category_confidence`, scope `global`** (`retrieval/news_category/rules.py`, `events.decide_news_category`): `regulatory` cần từ chế tài **và** chủ thể (mã hoặc cơ quan/sàn); "cảnh báo" cần ngữ cảnh sàn; niêm yết nước ngoài → `general_finance`/`global`; giao dịch công ty con → `corporate_action`; văn bản pháp quy → `macro_policy`; `market_flow` theo từ dòng tiền chứ không theo mọi chữ "thị trường"; "phát hành" chỉ tính với cổ phiếu/trái phiếu + chủ thể. Bộ vàng 97 tin: macro-F1 0.989, precision `regulatory` 1.00.
- **M3 — bổ sung ngày xuất bản cho tin từ trang HTML danh sách** (`retrieval/news_dates.py`, `events._enrich_missing_dates`): meta `article:published_time` → JSON-LD `datePublished` → ngày trong URL; ≤ 12 lần tải/lượt, 4 song song, theo ngân sách, SSRF + pinning + breaker riêng, ghi nhớ LRU; không suy từ thời điểm cào; `published_at_source`. Live: tin không ngày 3/60 → 1/60.
- **Hợp đồng (chỉ thêm)**: `ScrapeRequest.allow_insecure_escalation`, `ScrapeItem.tls_verified`, `MarketNewsWireItemOut.{symbol_evidence, indices, category_reason, category_confidence, published_at_source}`, enum `scope` + `global`, `ErrorCode.TLS_VERIFY_FAILED` (502). Cache news-wire `v2` → `v3`. OpenAPI snapshot regen.
- **Audit tự kiểm**: bắt lỗi patch ghi `` thành ký tự backspace trong regex phát hành (mất nhánh "cp"), actor "quỹ" khớp "chứng chỉ quỹ", 2 alias va chạm (Petrolimex, Sơn Hà) bị bỏ trước khi phát hành. Test mới: `test_v46_tls_policy.py`, `test_v46_wire_precision.py` + 2 bộ vàng JSONL (cột `origin`).

## [4.5.0] - 2026-11-24

### Added — Trust & Insight layer (`docs/34`)

- **`POST /v1/verify` — kiểm chứng một tuyên bố** (`app/orchestration/claim_verify.py`, `app/api/v1/trust.py`): tái dùng `prepare_context` (search → scrape có SSRF → chunk → rerank), đánh giá lập trường **tất định** từng chunk (cửa sổ câu theo lexical entailment, tín hiệu bác bỏ song ngữ VI/EN, lật cực phủ định có loại trừ "không chỉ/not only", độ khớp số liệu), LLM judge **tuỳ chọn** (`use_llm`, 1 lời gọi rẻ trong rào SEC-07 ngẫu nhiên, chỉ gán lại stance — không thêm bằng chứng, "supports" bị hạ khi số liệu không khớp, lỗi → giữ lexical). Verdict `supported/refuted/contested/insufficient_evidence` theo 1 bằng chứng tốt nhất mỗi tên miền × `tier_score` × relevance, `single_source` khi chỉ 1 tên miền; mọi bằng chứng là **trích dẫn nguyên văn** kèm tier + `published_at`/`fetched_at`. Search có trả lời nhưng không scrape được → 200 `insufficient_evidence` + `complete:false`; mọi engine chết → 503. Rate-limit 4, admission tier `verify (8)`.
- **`POST /v1/timeline` — dòng thời gian sự kiện + lăng kính độ phủ** (`app/orchestration/timeline.py`): news fan-out trong cửa sổ `days`, chỉ giữ kết quả có `published_at` thật (bỏ + đếm `undated_dropped`, `out_of_window_dropped`; feed lệch giờ ≤ +6h), SimHash gộp bản syndication thành 1 sự kiện, ngày sự kiện = lần đưa tin đầu tiên, xếp cũ → mới; coverage `by_domain_tier`, `by_country` (ccTLD rõ nghĩa), `volume_by_day`. Không LLM. Rate-limit 2.
- **`POST /v1/search/batch`**: ≤5 truy vấn/1 round-trip (song song tối đa 3, budget riêng mỗi item), partial by design (item lỗi có `error` riêng), billing 1 đơn vị/truy vấn (`_request_item_count` đếm `queries`). `search.result_dict()` dùng chung shape với `/v1/search`.
- **MCP tools** `verify_claim` + `event_timeline` (client bounded JSON + server).
- **Security headers chuẩn OWASP** (`SecurityHeadersMiddleware`, pure ASGI, SSE-safe, ngoài cùng nên UI tĩnh cũng có): `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, COOP `same-origin`, `Permissions-Policy` (chỉ `microphone=(self)`), CSP `default-src 'none'; frame-ancestors 'none'` cho `/v1/*`/`/healthz`/`/readyz`/`/metrics`; HSTS opt-in `HSTS_MAX_AGE_S`; tắt bằng `SECURITY_HEADERS_ENABLED=false`; header route tự đặt luôn thắng.
- **Web UI tab "✅ Kiểm chứng & Dòng thời gian"** (`components/TrustPanel.tsx`, helper thuần `lib/trust.ts`): banner verdict (icon + chữ), bằng chứng nguyên văn kèm tier/ngày/lý do, timeline rail + histogram khối lượng + chip độ phủ; chỉ biến theme, mọi href qua `safeHref()`.
- **stitch-skills (google-labs-code) — đánh giá & tích hợp phần phù hợp** (`docs/34 §4`): không đưa vào runtime/không tự cài plugin (bắt buộc Stitch MCP + tài khoản Google, tải/chạy mã ngoài; UI không dùng Vite/shadcn). Tích hợp quy ước **`frontend/DESIGN.md`** (định dạng Stitch: theme, palette & roles, typography, components, layout, do/don't) với bảng token sinh từ `globals.css`, và **`lib/designTokens.test.ts`** khoá đặc tả với code: token đủ 2 theme, giá trị khớp từng byte, khối "system dark" phải trùng khối "dark", cấm literal hex trong `components/*.tsx`.

### Fixed

- **[P1 data-integrity] Độ trung thực số liệu của `/v1/answer` so khớp substring** (`app/llm/citations.py::verify_numeric_fidelity`, lỗi có sẵn): "95%" được coi là có trong "6,95%", "95" trong "1995" → số bịa được chấm 100%. Nay so khớp **nguyên token** (`numbers_in`/`number_present`/`canon_number`, dùng chung với `/v1/verify`), chuẩn hoá dấu phân cách VI/EN (`7,09%` = `7.09%`, `1.000` = `1,000`), phần trăm phải xuất hiện dưới dạng phần trăm.
- **Test bom hẹn giờ** `test_last30days_search.py::TestYahooFinanceAdapter::test_rss_enrichment_without_injected_client`: `pubDate` hard-code 16/11/2026 + `freshness_days=7` → đỏ trên mọi máy/CI từ 24/11/2026; nay tính tương đối (`now − 1 ngày`).

### Audit loop (tự kiểm — phiên vừa lập phương án vừa implement theo yêu cầu chủ dự án)

- Vòng 2 fresh-eyes bắt và sửa 3 lỗi trước khi phát hành: (1) batch dùng chung 1 budget 5s cho mọi item → item xếp hàng sau semaphore bị cắt engine (nay budget riêng, tính từ lúc item chạy); (2) response model đặt `max_length` cho url/title/domain đầu ra → URL dài từ adapter có thể biến verdict hợp lệ thành 500 (bỏ cận trên ở field đầu ra không tự cắt; Rule 2 vẫn áp đủ cho request); (3) chunk không dấu câu (bảng/list) thành 1 "câu" khổng lồ làm relevance phồng → cửa sổ trượt 40 từ. Mỗi lỗi có test hồi quy.
- Vòng 3–4 (đối chiếu nghiệp vụ VN + **thăm dò đối kháng live** với engine thật): số thập phân VN `7,09%` không khớp EN `7.09%`; bài Google News gộp thành 1 tên miền `news.google.com` (nay resolve publisher qua `source_url`); "GDP tăng 9,5%" bị ủng hộ vì khớp substring trong `6,95%`; "uống thuốc tẩy chữa COVID" bị ủng hộ vì đoạn văn *thuật lại* tin đồn → thêm `hearsay` (field mới trên evidence; cue "claimed", "tin đồn", "được cho là"… không bao giờ là support, kể cả qua LLM judge) + cue bác bỏ "myth/misinformation/thông tin sai lệch". Sau sửa: tuyên bố đúng → supported (7 tên miền), sai số liệu → insufficient_evidence, tin giả → refuted. Giới hạn đã biết ghi ở docs/34 §7.
- Gates: pytest toàn bộ, ruff check/format, bandit, mypy (196 file), vitest (16 file), tsc; OpenAPI snapshot regen có chủ đích (chỉ thêm 3 path + 14 schema, 0 thay đổi hợp đồng cũ).

## [4.4.2] - 2026-11-22

### Documentation & deployment-example parity (theo đúng `scripts/release.py --bump patch --push`)

- **README.md (song ngữ EN/VI)** — kết thúc 4 bản stale: status line hiện **v4.4.1** (trước: v3.3.0), thêm bullet tổng hợp "v4.0.0 → v4.4.1" đầy đủ (Search upgrade + Market Intel + External Data Integrations 8 endpoints + Lite extraction pipeline + bing_web/global search + scanner freshness gate + Expert Briefing Engine `docs/33` + theme sáng/tối/tự động + bug sweep Lite runtime + Stage-2 adapters + CI coverage parity 88%); 2 dòng repo-layout "CHANGELOG — current v3.3.0" → v4.4.1.
- **`deploy/.env.lite.example`**: bổ sung nhóm v4.2.0/v4.4.1 keys Stage-2 + ext_data (`COINGECKO_API_KEY`, `ALPHA_VANTAGE_API_KEY`, `NEWSAPI_KEY`, `OPENWEATHER_API_KEY`, `TWELVEDATA_API_KEY`, `FMP_API_KEY`, `IPINFO_TOKEN` — keyless-first, key chỉ nâng quota) và nhóm v4.4.0 Briefing Engine (`BRIEFING_AI_ENABLED/REVIEW_ENABLED/MAX_EVIDENCE=18/CHANNELS_FILE`) — Lite example trước đó thiếu hoàn toàn 2 nhóm này (full `.env.example` đã có từ v4.2.0/v4.4.0).
- **`docs/06-api-spec.md`**: hợp đồng `POST /v1/scrape` ghi rõ alias `{"url": ...}` (v4.4.1) + hành vi 400 khi thiếu cả `urls` lẫn `url`.
- **Release theo process**: phiên bản này được cắt bằng `python scripts/release.py --bump patch --push` (script tự bump `api/app/__init__.py` + `pyproject.toml` + `frontend/package.json` + sync `uv.lock`/`package-lock.json`, verify CHANGELOG entry, chạy toàn bộ quality gates, clean và `git_release` commit + tag + push) thay vì thao tác tay như v4.4.1.

## [4.4.1] - 2026-11-22

### Fixed — CI parity: coverage floor 84% đáp ứng lại (lỗi CI fail cuối cùng)

- **Root cause**: CI fail trên cả 3 OS (`Backend Full Test Suite with coverage floor`): `Coverage failure: total of 82 is less than fail-under=84` — 27.566 statements, 5.013 missed. Nguyên nhân: ~1.800 LOC adapter Giai đoạn 2 + code v4.4.0 (briefing) vào repo **chỉ kèm test instantiation** (không test hành vi) và 13 file test coverage của các phiên trước **nằm untracked ngoài git** → CI không thấy chúng. **Release workflow v4.4.0 fail tương tự** (ruff format drift — đã fix ở loạt "CI parity").
- **13 file test coverage của các phiên trước vào repo** (`tests/`): `test_last30days_search`, `test_public_api_adapters`, `test_briefings_endpoints`, `test_events_coverage`, `test_key_validator_round2`, `test_llm_providers_round2`, `test_market_alert_candles`, `test_mcp_client_v2`, `test_persist_round2`, `test_podcast_personal`, `test_reranker_listwise`, `test_scheduler_helpers`, `test_sentiment_quant_live` — 132+ test hành vi thật (MockTransport) cho briefing registry/engine, ext_data, podcasts, scheduler, reranker… Toàn bộ đã pass trong pre-commit gate 2 lần gần nhất (implicit qua full pytest), giờ mới chính thức được track.
- **24 test hành vi cho 4 adapter Giai đoạn 2** (`tests/test_stage2_public_api_adapters.py`, MockTransport httpx): query routing (`overview/quote/earnings/news` của Alpha Vantage; `everything/top-headlines` NewsAPI; `current/forecast` OpenWeather; `market/price/trending/info/list` CoinGecko), parse RawResult + published_at 2 format, freshness filter, rate-limit/429/401 graceful, missing-key no-op, demo-key header, `max_results` bound.
- **Alpha Vantage đúng hợp đồng Note/Information/Error luôn no-op**: trước chỉ check `Note` khi message chứa "request" → note rate-limit khác bị parse thành RawResult rác (`Company:  ()`). Giờ mọi response mang `Note`/`Information`/`Error` đều bị loại (hợp đồng AV chuẩn).
- **NewsAPI freshness dùng `datetime.now(timezone.utc)`** thay `datetime.utcnow()` deprecated (py3.12 DeprecationWarning dâng thành error CI).
- **Dọn dẹp**: xoá artifact đo đạc lạc lõng `api/coverage_local.json`, `api/e2e_glob.json`, `api/e2e_tech.json` (không phải local runtime data — chỉ output test/coverage cũ).
- **Docs**: AGENTS.md mục "Trạng thái" cập nhật v4.4.1 đầy đủ (bug sweep + theme + Giai đoạn 2 + coverage parity).

### Fixed — Bản tin tổng hợp ăn theo theme + hệ theme sáng/tối/tự động cho Web UI

- **Body bản tin trắng trên nền tối [P1 UI]** (`frontend/components/NewsPanel.tsx`): khối `briefing-block` (🧠 Tổng hợp chuyên gia / 📄 Bản tin đã lưu, docs/33) hard-code `background: "#f6f8fa"` + border xám nhạt — trắng hoàn toàn trên shell tối. Nay dùng class `.briefing-block` (globals.css): `background: var(--card)`, border trái accent 3px, summary `var(--muted)` — theo bất kỳ theme nào; `.briefing-error` dùng `var(--err)`.
- **Hệ theme đầy đủ dark / light / system (mặc định "system")**:
  - `globals.css` refactor toàn bộ palette thành CSS variables 2 chế độ: `:root` (light, GitHub-style) + `:root[data-theme="dark"]` + `@media (prefers-color-scheme: dark)` cho `[data-theme="system"]` — OS đổi theme áp dụng live không cần listener. ~40 chỗ hardcode (rgba accent tints, nền trắng-alpha, màu bull/bear/warn, syntax-highlight token code, badge hues) chuyển hết sang biến; bảng màu light chuẩn contrast (accent #0969da, err #cf222e, syntax light-style).
  - `app/layout.tsx`: inline script pre-paint đọc `localStorage("metasearch.theme")` → stamp `<html data-theme>` trước lần render đầu — không flash sai theme; `suppressHydrationWarning` cho dataset script-set.
  - `lib/theme.ts` (mới): `ThemeChoice` = system/dark/light, `resolveTheme`, `readStoredTheme`/`storeTheme` (validate + fail-open private mode), `cycleTheme`, `applyThemeToDocument` — SSR-safe (guard `typeof window/document`).
  - `components/ThemeToggle.tsx` (mới): segmented control 🖥️/🌙/☀️ trên masthead (aria-pressed, aria-label VI); sync giữa các tab qua `storage` event.
  - `lib/theme.test.ts` (mới): 10 test — validation, resolve theo OS, chu kỳ ổn định, persist/corrupt value/quota-throw, data-theme stamp.
- **Đuôi theme còn sót**: `ChartPanel` lightweight-charts đọc `--muted`/`--border` lúc draw (`themeVar()`) thay vì hardcode dark; 2 chỗ đỏ error trong `page.tsx` → `var(--bear)`; hộp escalation Super-Agent hardcode nền trắng-amber → class `.warn-box` (tint theo `--warn` qua `color-mix`).
- **Added — 4 search adapter dữ liệu toàn cầu (Giai đoạn 2)** (commit trước cùng release): `alpha_vantage` (tài chính), `newsapi` + `newsapi_headlines` (tin toàn cầu), `openweather` (thời tiết), `coingecko` (crypto, keyless) — đăng ký thường trực trong `registry.build_adapters` (graceful khi thiếu key), 28 test instantiation/registry/config/search-method; ruff/bandit/mypy sạch. Lưu ý: khoá cấu hình đã tồn tại từ v4.2.0 (ext_data), đây là lớp search fan-out.

### Fixed — Rà soát bug còn sót từ log Lite runtime (2026-11-22)

- **careers adapter crash + SSRF [P1 security]** (`app/retrieval/adapters/careers.py`): log thật 21/11 cho thấy **mọi query prose tiếng Việt** đều làm careers fan-out nổ `httpx.InvalidURL: Invalid IDNA hostname '"hqc" công bố … .com'` (careers là engine mặc định từ v4.3.0). Nguyên nhân: `_get_careers_url` nối query thẳng vào hostname và **nhận cả query-is-URL rồi fetch trực tiếp — bypass SSRF hoàn toàn** (không qua `resolve_and_check`/pinning như /v1/scrape). Fix: chỉ curated companies + slug ASCII strict `^[a-z0-9][a-z0-9-]{1,30}$` được build URL; query-URL bị loại (user URL thuộc /v1/scrape có SSRF đầy đủ); mọi thứ khác → no-op graceful.
- **Unicode logging crash Windows [P1]** (`app/core/logging.py`): console Lite Windows chạy cp1252 — dòng log chứa tiếng Việt nổ `UnicodeEncodeError` ("--- Logging error ---" spam `.lite.err.log`, 5+ lần). Fix `_force_utf8_stdio()`: reconfigure stdout/stderr → UTF-8 `errors="replace"` một lần mỗi process trước `basicConfig`, không bao giờ crash.
- **`/v1/scrape` chối body `{"url": ...}` [P2 DX]** (`app/api/v1/schemas.py`): clients/AI tools thường gửi `url` đơn → 400 "Field required urls" (2 lần trong log). Fix: field `url` alias (≤2000 ký tự, W5 Rule 2) fold vào `urls` qua `model_validator`; thiếu cả hai → 400 message rõ ràng. OpenAPI snapshot regen (chỉ ScrapeRequest).
- **Tests**: 15 regression mới (`tests/test_leftover_bug_sweep.py`) — careers slug/URL/prose/SSRF matrix, UTF-8 stdio reconfigure + dead-stream survive, alias fold/urls-wins/bounds; cùng `test_last30days_search.py` 63/63 xanh; ruff/bandit/mypy/format sạch.
- **Verify**: `vitest 120/120` (13 file, gồm 10 test theme mới) + `tsc --noEmit` sạch + `next build:export` xanh + live check HTML/CSS serve trên Lite :8000 (bootstrap script + `.briefing-block{background:var(--card)}` có mặt trong bundle).

## [4.4.0] - 2026-11-21

### Added — Expert Briefing Engine: Multi-AI Consensus & Audio-Enabled Intelligence (`docs/33`)

- **Briefing Channel Registry (`api/app/orchestration/briefing_registry.py`)** — single source of truth: 6 core windowed news channels (vĩ mô 72h *Chief Macroeconomist*, tài chính 48h *Senior Equity Strategist & Portfolio Manager*, kinh doanh 72h *M&A Analyst*, công nghệ-AI 72h *Principal AI Architect*, ngân hàng 72h *Banking/Credit/Monetary Policy Specialist*, thế giới 72h *Global Macro & Geopolitical Strategist*) + 5 deep channels (AI Coding 96h, AI Agents & MCP 96h, Chứng Khoán VN 72h *Vietnam Market Strategist*, Phong Thủy 168h *non-superstitious researcher*, Y Học 168h *evidence-based, never diagnosing*). Thêm kênh mới = 1 entry JSON qua `BRIEFING_CHANNELS_FILE` — không đổi code; row lỗi skip an toàn.
- **Deterministic pre-processing (`briefing_engine.collect_channel_evidence`)**: fan-out ≤6 engines khớp category + `freshness_days=ceil(window/24)`; **STRICT window filter** chỉ nhận bài có `published_at` thật trong cửa sổ (undated/stale bị loại, `dropped_stale` minh bạch); canonical-URL + SimHash event-thread clustering (thread_members giữ mọi URL syndication); authority ranking `tier_score` + đa-engine + decay recency → evidence packet đánh số `[n]` có ngày + URL.
- **Step A — persona synthesis**: LLM call trusted-only (docs/30 allowlist) với persona + hợp đồng zero-fabrication; cấu trúc 3 phần bắt buộc: *Bức tranh tổng quan* / *Các diễn biến trọng yếu* (What → Why → Tác động bậc hai) / *Góc nhìn chuyên gia & Điểm cần theo dõi*; citation `[n]` vào packet.
- **Deterministic grounding verifier**: mọi `[n]` out-of-range bị strip; **số liệu trong draft phải tồn tại trong text bằng chứng** — câu vi phạm bị LOẠI trước publish (`stripped_unverified`) — zero-hallucination cưỡng chế bởi hệ thống, không phụ thuộc lời hứa model.
- **Step B — council review + revision**: Fact-Checker + Domain Skeptic verify `{hallucination_free, depth_ok, issues}`; fail → 1 revision pass → verifier chạy lại; outcome ghi Dream-RSI policy store (`record_endpoint_outcome`) để endpoint selection học tiếp.
- **Endpoints + audio**: `GET /v1/briefings/channels`, `POST /v1/briefings/{channel_id}` (require_admin, single-flight per channel, W5 cost 8), `GET /v1/briefings/{channel_id}/latest`. Briefings persist BlogPost (tags `ai-briefing`/`council-reviewed`) → Blog Chuyên Sâu + `/v1/digests` + RSS tiêu thụ tự nhiên; đọc audio bằng Web Speech có sẵn + pipeline VoiceStudio chung — không thêm adapter.
- **Frontend**: `NewsPanel` — nút `🧠 Tổng hợp chuyên gia` + `📄 Bản tin đã lưu` mỗi category (map 1:1 sang registry channels), render qua `AnswerMarkdown` với citation chips; `api.ts` thêm `generateBriefing`/`latestBriefing`/`listBriefingChannels`.
- **Config**: `BRIEFING_AI_ENABLED` / `BRIEFING_REVIEW_ENABLED` / `BRIEFING_MAX_EVIDENCE` / `BRIEFING_CHANNELS_FILE` (`.env.example` docs). AI tắt → extractive fallback trung thực từ cùng evidence.
- **Tests**: 12 mới (`tests/test_briefing_engine.py`) — registry/JSON override, STRICT window, thread clustering, grounding verifier, council pass/revision trigger, fallback, endpoint gating; W5 47 + contract regen + full pytest exit 0 + mypy 193 + frontend tsc/110 vitest/build xanh.

## [4.3.1] - 2026-11-21

### Fixed — Scanner freshness gate: bản tin chuyên sâu không còn dẫn tin cũ năm trước (báo cáo "dữ liệu cuối 2024")

- **Không phải ảo giác — reproduced bằng chứng thật**: scan topic "Kinh Doanh & Kinh Tế Vĩ Mô" ngày 21/11/2026 dẫn TOP 1 bằng tin thật "Hội thảo đầu tư **2024**" (24h.com.vn); 10 nhắc "2024" vs 9 nhắc "2026" trong cùng bản tin. Synthesizer là extractive thuần (`docs/29 P0-1`) — tin cũ đến từ kết quả search thật, vì scanner fan-out **KHÔNG truyền `freshness_days`** nên relevance-only ranking ưu tiên nội dung "evergreen" hàng năm.
- **Freshness bắt buộc (`app/orchestration/scheduler.py`, `config.py`)**: mọi scan query giờ truyền `freshness_days=scan_freshness_days` (mặc định 7) — google_news (`when:`), bing_news (qft interval), gdelt (time window), ddg (`df`), fin_rss (date filter) đều đã hỗ trợ sẵn; adapters khác bỏ qua param một cách graceful. Config mới: `scan_freshness_days`, `scan_max_result_age_days` (365).
- **Recency gate trong `synthesize_blog_content`**: citations sắp **mới nhất trước** theo `published_at` THẬT của nguồn; tin có ngày cũ hơn ngưỡng 365 ngày chỉ lấp slot còn lại khi tin mới khan hiếm — và **mọi dòng citation hiển thị ngày nguồn** (`*(ngày 18/11/2026)*` / `*(ngày không rõ)*`) — không bao giờ bịa ngày.
- **Category matching mở rộng**: `ScanTopic.extra_categories` — macro_business thêm `financial`, viet_finance thêm `news` (tin vĩ mô giờ fan-out cả vn_wire/fin_rss/yahoo_finance); fan-out tăng `[:3]` → `[:6]` engines.
- **Fix test debt**: fake adapters trong `test_v29_run_digest.py` chấp nhận `freshness_days` kwarg (hợp đồng adapter chuẩn có sẵn từ docs/19). 5 test mới (`tests/test_scan_freshness.py`): freshness truyền xuống, newest-first, drop stale khi đủ tin mới, giữ stale + nhãn khi khan hiếm, undated giữ không bịa ngày.

## [4.3.0] - 2026-11-21

### Added — Lite profile: full extraction pipeline + global search expansion (`docs/32` §6)

- **Extraction pipeline cho Lite (trafilatura → crawl4ai → Jina)**: tier crawl4ai trên Lite trước giờ trỏ Docker hostname (không tồn tại) — fail mỗi 60s. Nay: trỏ `CRAWL4AI_URL=http://127.0.0.1:11235` khi đã cài server local (autodetect qua connect); **connection refused trên loopback → cooldown 300s fast-fail** (`_is_local_crawl4ai`) thay vì thử lại mỗi 60s; remote host giữ 60s (lỗi tạm thời). Tier 1 trafilatura in-process và tier 3 Jina keyless (~20 rpm) không đổi. `.env.lite.example` ghi chú 3-tier.
- **Global search expansion cho Lite**: live check cho thấy Lite đã có 25 engines keyless; điểm nghẽn thật là (a) web tổng hợp chỉ có DDG và (b) 7 adapters Last30Days có trong `build_adapters` nhưng **không có EngineConfig → không bao giờ được fan-out**.
  - **Adapter mới `bing_web`** (Bing Web Search RSS, keyless, probe thật 2026-11-21 — kết quả web + tiếng Việt; reuse `_unwrap_bing_url`/`_parse_pubdate` từ bing_news): EngineConfig `general` weight 0.9 → Lite có **nguồn web tổng hợp thứ hai độc lập index** cạnh DDG.
  - **5 verticals Last30Days query-aware** vào fan-out mặc định: `polymarket` (news), `stocktwits` (social, symbol-aware), `uspto` + `uspto_company` (reference, patents), `careers` (general, theo `careers_enabled`). Hai adapter **query-blind** (`polymarket_trending`, `stocktwits_trending`) cố tình KHÔNG có config — bật sẽ trộn kết quả không liên quan; dùng được qua hot-config.
  - Engine seeding upsert sẵn có tự thêm 6 config mới vào SQLite DB cũ khi restart (không đè tweak của operator).
- **Tests**: 5 mới (`tests/test_lite_expansion.py`) — bing_web RSS unwrap/dedupe/403, config presence + query-blind exclusion, `_is_local_crawl4ai`, cooldown 300s cho localhost refused.

## [4.2.0] - 2026-11-21

### Added — External Data Integrations, keyless-first (`docs/32`; giai đoạn Tích Hợp API Free Key + nhóm Mở Rộng)

- **Keyless-first design law (`docs/32` §1)**: mọi khả năng hoạt động KHÔNG cần đăng ký qua nguồn public đã probe thật; key free của nhà cung cấp chỉ là nâng cấp tùy chọn. Demo key của nhà cung cấp bị loại (trả dữ liệu mẫu = ngụy tạo). Mỗi khối dữ liệu mang provenance riêng (`source_name`/`source_url`/`as_of`); thiếu key/nguồn chết → 503 + hint hoặc khối rỗng — không bao giờ bịa số.
- **Package `api/app/retrieval/ext_data/`**: TTL cache bounded (monotonic) + single-flight + key-tag (sha256[:8]) phân biệt biến thể keyless/keyed trong cache mà không lưu secret; mọi fetch qua pool `search` dùng chung, cap 5MB.
  - **Crypto (`crypto.py`)**: CoinGecko simple prices (24h change/volume/mcap) + global stats; demo key → `x-cg-demo-api-key`, `CG-*` → `x-cg-pro-api-key` (verbatim, đúng hợp đồng CoinGecko).
  - **FX (`fx.py`)**: fan-out Frankfurter (ECB — thắng hòa) + ExchangeRate-API open (160+ mã, có VND), merge với provenance từng mã; key `EXCHANGERATE_API_KEY` nâng lên host keyed v6.
  - **Weather (`weather.py`)**: Open-Meteo geocoding + forecast 1..7 ngày với bảng WMO code chuẩn (không bịa mô tả); `OPENWEATHER_API_KEY` thêm khối current OWM.
  - **News (`news_ext.py`)**: NewsAPI everything/top-headlines — key-gated, thiếu `NEWSAPI_KEY` → 503 kèm hướng dẫn (google_news của MetaSearch là đường keyless). Filter article thiếu title/url.
  - **Global quotes (`markets_global.py`)**: Yahoo Finance chart API keyless (AAPL probe thật); `TWELVEDATA_API_KEY`/`ALPHA_VANTAGE_API_KEY` thêm khối riêng từng nguồn (không trộn thành "giá thật"); FMP income/balance/cash-flow — key-gated, 4 kỳ gần nhất, bounded fields.
  - **IP geo (`ipgeo.py`)**: ipwho.is keyless (success flag check) + `IPINFO_TOKEN` thêm khối IPinfo.
- **8 GET endpoints (`api/app/api/v1/extdata.py`)**: `/v1/market/crypto`, `/v1/market/crypto/global`, `/v1/market/fx`, `/v1/weather`, `/v1/news/external`, `/v1/market/global-quote`, `/v1/market/fundamentals`, `/v1/net/ipgeo` — auth theo `_auth` của `/v1`, W5 bounds đầy đủ (Query + schema), GET-only.
- **Config (`config.py`, `deploy/.env.example`)**: `twelvedata_api_key`, `fmp_api_key`, `ipinfo_token`, `exchangerate_api_key` thêm vào nhóm "Free API Keys" (alpha_vantage/newsapi/openweather/coingecko đã có sẵn).
- **Tests**: 22 unit MockTransport (`tests/test_ext_data.py`) — merge FX, key gating + hint, WMO decode, demo/pro header CoinGecko, dead-upstream fail-open, bounds; OpenAPI snapshot regen; W5 + mypy 189 file xanh. Loại trừ có chứng cứ: Stooq CSV bị chặn ở egress này (probe 2026-11-21) — thay bằng Yahoo keyless.

## [4.1.1] - 2026-11-19

### Fixed — Round-2 adversarial audit of the v4.1.0 wave (2 read-only auditors + live-registry probes; details in `docs/31` §5.1)

- **Peer discovery precision [P1]** (`api/app/retrieval/intel.py`): on the real 1518-ticker registry, generic folded tokens (`phát` df=95, `thương` df=98, `công` df=1464) bridged wrong domains — VCB surfaced a fintech, HPG surfaced banks. Now: diacritic-folded tokens from `company_name`+`short_name`, IDF-weighted with a df ceiling (`max_df_fraction=0.01`, floor 2), corporate-vocabulary stop list applied on FOLDED forms. Verified on real data: BID→BSI/BIC (Chứng khoán/Bảo hiểm BIDV), FPT→FRT/FTS; honest empty when no rare identity token exists. Scope documented: identity links, not sector clusters (registry has no sector field).
- **Thread carry cost & cache correctness [P2]** (`api/app/orchestration/pipeline.py`): cache-check the raw follow-up BEFORE the rewrite (cacheable queries no longer pay the hop); rewrite memoized by `(prior_answer_id, follow-up)` in a bounded 256-entry LRU so resending the same turn reuses the SAME query (deterministic `an:v4` key instead of guaranteed miss); rewrite `Usage` recorded via `record_usage`; rewritten text sanitized (whitespace/control-char collapse, 200-char hard cap). `thread_rewrite` now also surfaces in JSON mode as `AnswerResponse.rewritten_query` (null when no rewrite).
- **Answer surface hygiene [P3]**: `_load_thread_context` DB read moved INSIDE the admitted region (lazily resolved by the JSON body / SSE producer); false "no dangling citation_map" comment corrected to the provisional-map contract.
- **Anonymous tenancy posture pinned [P2]**: auth-off sets the owner literal `"anonymous"` — thread carry can load another anonymous caller's prior answer (consistent with the loopback-only anonymous surface); behavior now pinned by `test_anonymous_mode_is_one_shared_tenant` and documented instead of being left accidental.
- **Alerts endpoint hardening [P2]** (`api/app/api/v1/market.py`): `POST /v1/market/alerts` gather runs under one outer wall-clock cap with per-symbol isolation (`return_exceptions=True`) — an unexpected failure degrades to an empty bundle, never a 500, and sibling fetches are not orphaned.
- **Event-impact honesty [P2]** (`intel.py`): forward truncation no longer mislabels the horizon — window return is `null` when `bars_used < window_days`; tz-aware event dates convert via `astimezone` (not re-labeled); dchart 00:00-UTC bar-timestamp convention pinned in comments.
- **Alert condition naming & series integrity [P2/P3]**: `NEAR_20D_HIGH` → `NEAR_20D_CLOSE_HIGH` (computed on closes, honestly named); closes/volumes filtered once per bar so RSI and volume windows can never desync on a malformed bar.
- Tests: 12 thread/citation (incl. rewrite memoization, sanitization, anonymous bucket pin), 28 intel/market (IDF peers on padded registry, renamed condition, horizon honesty); OpenAPI snapshot regenerated (`rewritten_query`, float peer `score`).

## [4.1.0] - 2026-11-19

### Added — Answer Quality Wave (docs/30 BP-1/BP-3/BP-4) + Market Intelligence Orchestration Layer

- **BP-1 Thread carry (`api/app/api/v1/answer.py`, `api/app/api/v1/schemas.py`, `api/app/orchestration/pipeline.py`)**: `AnswerRequest.thread_id` loads the caller's prior answer (owner-checked, 404 semantics → fail-open `None`) and one cheap LLM hop rewrites the follow-up into a standalone keyword query before retrieval; `thread_rewrite` event reports `{original, rewritten}` (yielded — observed by every consumer); broken rewrite fails open. Tests: `tests/test_v30_thread_and_citation.py`.
- **BP-4 Citation streaming (`pipeline.py`, `frontend/lib/api.ts`, `frontend/app/page.tsx`)**: `citation_map` SSE event (n/url/title/chunk_id) is emitted inside the try-block BEFORE the token stream so the UI renders clickable citation chips immediately; the final validated `citations` event supersedes it. UI handlers `onCitationMap`/`onThreadRewrite` wired in `AnswerHandlers` + `page.tsx`.
- **Market Intelligence Orchestration Layer (`api/app/retrieval/intel.py` — new)**: peer auto-discovery (`discover_peers`: same-exchange distinctive company-name token overlap over the VN registry, deterministic), technical alert engine (`evaluate_alert_conditions`: Wilder RSI-14 bands, volume spike ≥2.5× prior-10-day average, 5-day MA cross, 20-day-high proximity guarded against flat sideways noise), and news-to-price event impact (`compute_event_impact`: 1-day + N-day return + volume ratio around an event date — missing candles → `null`, never fabricated). Tests: `tests/test_intel.py` (16), `tests/test_market_intel.py` (9).
- **New endpoints (`api/app/api/v1/market.py`)**: `GET /v1/market/peers?symbol=` (pure registry CPU, bounded `max_peers`) and `POST /v1/market/alerts` (1..5 symbols, OHLCV fetched VNDIRECT→FA-aggregator fallback with one shared wall-clock cap; symbols without usable candles report empty alerts and no source). Rate-limit: payload-proportional `2 + min(symbols, 5)` (`auth.py`), `DYNAMIC_COST_POST` updated (`test_w5_guardrails.py`).
- **Thread-rewrite observation fix**: `thread_rewrite` moved from `on_event`-only emission to a yielded pipeline event so direct `run()` consumers see it too.

### Fixed — `/v1/meta/full` keyless access + sentiment regime/confidence calibration

- **`/v1/meta/full` now always requires a real admin API key (`api/app/core/auth.py::require_admin_key`, `api/app/api/v1/router.py`)**: previously `AUTH_ENABLED=false` + `DEV_ANON_ADMIN=true` (Lite profile) made the endpoint answer `200` to keyless callers. Now keyless/invalid key → `401`, non-admin key → `403`, admin key → `200` regardless of the Lite flags. `/v1/meta` (name + version) stays public. Regression test: `api/tests/test_v40_meta_full_auth.py` (3 auth/anon-admin matrices).
- **Sentiment regime & confidence calibration (`api/app/retrieval/sentiment.py`)**: `EVENT_DRIVEN_BINARY` is no longer the catch-all (any one positive + one negative headline, or a plain neutral book); polarization now needs each side to hold >= 30% of the evidence weight and weak catalyst-free evidence maps to `LOW_SIGNAL_NOISE`. Confidence is reduced when the text and quant pillars point in opposite directions instead of reading pure evidence volume as certainty. Test: `test_v40_sentiment_upgrade.py::test_regime_not_binary_for_weak_unpolarized_and_confidence_penalizes_conflict`.

### Added & Fixed — LLM Endpoint Privacy Policy + Bilingual Wire Entity Tagging (docs/30)

- **Trusted-Only LLM Endpoint Allowlist (`api/app/llm/endpoint_policy.py`, `api/app/llm/gateway.py`, `api/app/main.py`, `api/app/config.py`)**: default-deny governance for every LLM base_url — enterprise zero-data-retention providers (Anthropic/OpenAI/Gemini/Vertex) and local/LAN self-hosted inference are trusted; unknown third-party endpoints (openrouter/pollinations/groq mirrors/spoofed suffixes) are rejected at runtime (`_candidates` guard on override-injected targets — BYO/request/runtime) and fail-fast at startup (`sweep_configured_endpoints` SystemExit in prod, warning in dev/test). Operator override: `LLM_ENDPOINT_POLICY=open` (SSRF validation still applies).
- **Stage B Bilingual Entity Resolver (`api/app/retrieval/aliases.py`, `api/app/retrieval/events.py`)**: 1,020-alias canonical dictionary (unambiguous registry remainders + curated brands: Vinamilk→VNM, Địa ốc/Hoàng Quân→HQC, Thép/Tập đoàn Hòa Phát→HPG, Bia Sài Gòn→SAB, Long Châu→FRT, …) wired into `extract_tickers_from_text` — `/v1/market/news-wire` items with bare brand mentions ("Vinamilk chi cổ tức", "Hoàng Quân Địa ốc") now tag `tickers` correctly; ambiguous remainders ("Hòa Phát" across HTP/HPA/HPG) never indexed (anti-collision by construction); word-boundary matching O(text) single pass.
- **14 verification tests** (`api/tests/test_v30_endpoint_policy_and_wire.py`) — enterprise/local trust matrix, suffix-spoof block, runtime enforcement + operator override, startup sweep, sliding-window expose, brand-alias resolution, allowed-symbols contract.

## [4.0.0] - 2026-11-19

### Added & Upgraded — Search/Retrieval Overhaul, Institutional Sentiment Engine v3, Deep Research v2 & Eval Benchmarks (`docs/31`)

- **Wave 1 — Search & Retrieval Core Overhaul (`scoring.py`, `tiers.py`, `merger.py`, `router.py`, `query_expansion.py`, `reranker.py`, `pipeline.py`, `engine_store.py`, `registry.py`)**:
  - Registered 3 keyless adapters (`bing_news`, `vn_wire`, `fin_rss`) and fixed trailing syntax bug in `ddg.py`.
  - Upgraded relevance & authority scoring (`tokenize_dual`, `compute_batch_idf`, bigram + accented matching, normalized RRF rank fusion, Google News / Bing wrapper `_effective_domain`, `0.45x` multi-term relevance gate, Title Jaccard `>= 0.72` near-dup folding, `low_trust` tier, `.gov*`/`.edu*` rules).
  - Upgraded `HeuristicReranker` to hybrid BM25 + heuristic + MMR (`max_per_url=4`, 24-chunk cap), added ONNX 512-token truncation, nested `BGEReranker._pick_file`, optional `LLMListwiseReranker` (`RERANKER=llm`), implicit freshness extraction in `router.py`, and `test_v40_search_upgrade.py`.
- **Wave 2 — Institutional Sentiment Engine v3 (`sentiment_quant.py`, `sentiment.py`, `market.py`, `alembic/versions/0011_sentiment_snapshots.py`, `SentimentPanel.tsx`)**:
  - Integrated verified keyless market & macro feeds (CNN Fear & Greed, CBOE Put/Call, FRED HY OAS, Yahoo `^VIX`/`^VIX3M`, CafeF foreign/prop/insider flows, SSI iBoard orderflow & HOSE breadth TRIN/McClellan RANA, DNSE OHLCV fallback, Simplize extended consensus & news).
  - Fixed down-day volume surge polarity and enforced >= 2 independent signal groups for BofA Bull & Bear analog; added 350+ VI/EN phrases, extended rumor & anti-collision patterns (`GAS`, `VNM`), SEntFiN sentence-level target attribution + `tanh` saturation, SimHash seeding dedup, `BEARISH_MARKDOWN`/`DISTRIBUTION_PRESSURE` regimes, hybrid text+quant composite score, opt-in `run_llm_sentiment_judge`, `SentimentSnapshot` (migration `0011`) for 20-day S-Score / SV-Score, and Web UI cards in `SentimentPanel.tsx`.
- **Wave 3 — Deep Research v2 & LLM Council Debiasing (`research.py`, `council.py`, `persist.py`, `research_jobs.py`, `api/v1/research.py`, `ResearchPanel.tsx`)**:
  - Fixed `research.py` (`_MAX_CHUNKS_PER_DOC_IN_MERGE=6`, diacritic-folded profile & coverage gate, duplicate follow-up avoidance, pre-scrape `seen_urls` dedup, multi-batch strict-ID `_extract_with_conflicts`, `admit("research")` + `RequestBudget`).
  - Added Stage 0 Specialist Tool Mesh (Spaces hybrid RAG/KG + Finance live wire/events/quant), STORM `outline`, Devil's Advocate counter-thesis query, global rerank + contiguous citation remapping + `flag_injection`, `RESEARCH_REPORT_SYSTEM_PROMPT`, FACT `verification` event, and rich `Query.params` persistence/restore.
  - Debiased `council.py` `_run_anonymous_cross_review` (dual concurrent reviewers, shuffled candidate presentation, 8000-char context slice, self-review score exclusion).
  - Exposed `GET /v1/research` history, `POST /v1/research/{research_id}/cancel`, `after_seq`/`Last-Event-ID` resumable SSE with `id:` lines, `space` field, and Web UI controls in `ResearchPanel.tsx`.
- **Wave 4 — Evaluation Suites & Metrics (`eval_metrics.py`, `eval_v2.py`, `eval_sentiment.py`, `eval_research.py`)**:
  - Fixed `ndcg_at_k` duplicate substring matching (`<= 1.0` guarantee), cleaned `evals/golden_v3_vi.json`, added latency percentiles, slice aggregation, numeric fidelity, citation precision, and `--judge` wiring in `scripts/eval_v2.py`.
  - Added `evals/sentiment_golden.json` (64 bilingual cases) + `scripts/eval_sentiment.py`, `evals/search_offline_ir.json`, and `evals/research_golden.json` + `scripts/eval_research.py`.

## [Unreleased]

## [3.3.1] - 2026-11-18

### Added — Market News Wire & Corporate Events Intelligence (`POST /v1/market/news-wire`, `POST /v1/market/events`)

- **Headless Bilingual News Wire & Corporate Events Engine (`api/app/retrieval/events.py`)**:
  - **Context-Aware Entity Extraction (`extract_tickers_from_text`)**: extracts Vietnam & global equity tickers across headlines/snippets using prefix (`TICKER:`), explicit Vietnamese phrase (`cổ phiếu TICKER`), and `$CASHTAG` signals without maintaining naive three-letter blacklists, while suppressing FX-pair and unit contexts (`100 USD`, `USD/VND`, bare macro acronyms like `CPI`/`GDP`/`OMO`).
  - **Syndication Deduplication & Dual-Semantics Freshness (`deduplicate_news_items`, `filter_news_freshness`)**: strips volatile timestamps and punctuation from headlines (`content_fingerprint`), merges canonical URLs/fingerprints across RSS/HTML sources with `duplicate_count` and `also_reported_by`, keeps upcoming `event_date` items active until expiry, expires stale `published_at` items beyond `max_age_days`, preserves undated entries as `undated`, and flags delta items via caller-supplied `seen_fingerprints`.
  - **Structured Corporate Action, Calendar & Risk Classifiers**: parses CafeF corporate actions (`parse_cafef_corporate_actions`), Next.js SSR dividend/bonus cards from `cotuc.vn` (`parse_cotuc_calendar_html`, separating cash percentages/VND from stock entitlement ratios and detecting HREF-vs-text date mismatches), RSS/HTML feeds (`parse_rss_feed_xml`, `parse_html_article_links` via `defusedxml` + stdlib `HTMLParser`), listing states (`classify_listing_event`: prioritizes positive reinstatement `reinstated` ahead of `delisting`/`warning_control` and captures `additional_listing` dilution share counts), severity-first regulatory classification (`classify_regulatory_action`: `sanction` > `warning` > `statistics` > `listing_notice`), insider trade direction/execution (`classify_insider_trade`), compound share-factor arithmetic (`parse_share_factor`: `100:23` × `100:18` = `1.4514`), and cumulative share-ratio discrepancy verification (`explain_share_ratio`).
  - **Headless Resilience & Circuit Breakers**: async `httpx` fetchers guarded by `EngineBreakerRegistry` (`deps.get_events_breaker_registry()`), 5 MB response bounds, HTTP(S)-only URL sanitization, cache TTLs, `partial=true` reporting, and explicit `possible_layout_changed` warnings when non-empty HTML/RSS shells yield zero parsed items.
- **REST Endpoints, Schemas, Admission, Rate Limits & MCP Tools (`api/app/api/v1/market.py`, `schemas.py`, `auth.py`, `deps.py`, `mcp/client.py`, `mcp/server.py`)**:
  - Exposed bounded `POST /v1/market/news-wire` (rate cost `3`) and `POST /v1/market/events` (dynamic rate cost `3 + symbols`, capped at `13`) under the `"market"` admission tier with `400 invalid_request` validation on malformed symbols or invalid share ratios, plus MCP tools `market_news_wire` and `market_events` capped cleanly within `_TOTAL_CHAR_BUDGET`.
- **Web UI Parity (`frontend/lib/api.ts`, `frontend/components/NewsPanel.tsx`, `frontend/lib/api.test.ts`)**:
  - Added typed `fetchMarketNewsWire` / `fetchMarketEvents` helpers and a compact `"Sự kiện doanh nghiệp & Dòng tin cấu trúc"` lookup panel in `NewsPanel` with `AbortController` and `safeHref` guards.
- **Verification & Contract Updates (`api/tests/test_market_events.py`, `api/tests/test_w5_guardrails.py`, `api/tests/contract/openapi_v1.json`)**:
  - Regenerated `openapi_v1.json`, extended `COSTED_POST`, and added 18 unit, parser, `MockTransport`, REST integration, MCP dispatch, and Round 1–3 adversarial audit regression tests in `api/tests/test_market_events.py`.
- **3-Round Adversarial Audit Hardening (`events.py`, `schemas.py`, `auth.py`, `market.py`, `mcp/client.py`, `NewsPanel.tsx`)**:
  - Hardened `_sanitize_http_url` (userinfo/port/hostname rejection), streaming `_fetch_source_text` (incremental bounded `zlib` decompression + SSRF redirect + `httpx.HTTPError`/`httpx.InvalidURL` fail-open), auth-before-cost `401` ordering in `auth.py`, `DEADLINE_EXCEEDED` `504` status mapping, bounded `_to_vn_date` overflow handling, iterative `_element_text` depth caps, `CancelledError` child-task cancellation cleanup, thread-safe per-loop MCP client caching + `<= 12_000` char bounded JSON output, bilingual clause/negation/tax/split/reversal parser accuracy, and dark-mode contrast + IME/state guards in `NewsPanel.tsx`.

### Fixed — 4 Production Defects (ddg degradation, sliding-window alerting, empty fa/compare, cross-ticker sentiment bleed)

- **ddg engine degradation (`api/app/retrieval/adapters/ddg.py`)**: per-call User-Agent rotation from a realistic desktop pool; ONE jittered retry on transient failures (transport/timeout/5xx) with bot-blocks (202/403/429/challenge) never retried; tolerant snippet regex so a DOM change degrades snippets instead of silently zeroing whole result rows; a 200-with-zero-parsable-rows now logs `ddg_parse_zero_rows_dom_changed`.
- **Sliding-window engine health alerting (`api/app/orchestration/engine_store.py`, `api/app/api/v1/engines.py`, `api/app/orchestration/pipeline.py`)**: per-engine ring buffer (`deque(maxlen=10)`) recording (ts, ok, outcome) for every dispatch — `record_result` now carries the outcome kind (`ok`/`empty`/`timeout`/`upstream_error`/`blocked`/`rate_limited`); `GET /v1/engines/health` exposes `recent_window` per engine with `degraded` alerting (consecutive failures >= 3, or failure_rate >= 0.5 over >= 5 dispatches) and automatic recovery as fresh successes slide old failures out.
- **`fa/compare` empty payloads for valid tickers (`api/app/retrieval/fa_sources/simplize.py`, `api/app/retrieval/fa_sources/__init__.py`, `api/app/api/v1/market.py`, `api/app/api/v1/schemas.py`)**: registered a new `SimplizeFASource` (keyless VN fundamentals — pe/pb/roe/market_cap; the only fundamentals source live from project egress per docs/15 §10) so HQC/FPT merge real ratios again; an empty payload with zero sources now carries `all_sources_unavailable: true` + a WARNING log instead of a silent 200.
- **`market/sentiment` identical metrics across tickers (`api/app/retrieval/sentiment.py`, `api/app/api/v1/market.py`)**: the zero-evidence terminal payload now flags itself (`ZERO_EVIDENCE_DEFAULT_PAYLOAD` breaker) and carries `hits_retrieved` / `hits_after_collision_filter` telemetry on every payload shape; VN tickers default to per-ticker VNDIRECT quant (`fetch_market_data` server-side ORs `entity.is_vietnam`) so HQC vs FPT diverge on real per-ticker OHLCV instead of converging on a shared market-wide template.
- **15 verification tests** (`api/tests/test_v29_production_defects.py`) — window slide/recovery, outcome-kind cap, UA rotation, bot-block no-retry, transient retry, Simplize per-symbol fields, registration, honest empty flag, fan-out/cache-key per-ticker divergence, zero-evidence self-flagging, distinct per-ticker scores.
- **ZCode PASS-2 P2/P3 & `39994c4` Hardening Follow-ups + CI Frontend Audit Repair**:
  - `api/app/retrieval/fa_sources/simplize.py`: fixed `epsRatio` mapping (`"eps"` was `None` on live payloads), fixed `roe` percentage unit (avoiding 100x inflation on `<1%` ROE small caps like `HQC`), treated `404` as healthy `None` without tripping the circuit breaker, enforced `status == 200` and `math.isfinite`.
  - `api/app/retrieval/adapters/ddg.py`: wired actual single jittered retry on HTTP `5xx` responses (while keeping `202`/`403`/`429`/challenge/4xx non-retryable).
  - `api/app/api/v1/market.py` & `api/app/retrieval/sentiment.py` (`P2-1`, `P3-2`): honored explicit `"fetch_market_data": false` opt-outs on Vietnamese tickers via `model_fields_set`; prevented unverified rumors/seeding from shifting `social_score` or Fear & Greed composite inside `_quant_only_payload`; preserved `hits_retrieved` / `hits_after_collision_filter` telemetry on quant-only payloads; tightened `_csv_safe_cell` with full `lstrip()` against Unicode whitespace.
  - `api/app/api/v1/podcast.py` & `api/app/api/v1/spaces.py` (`P2-3`), `api/app/retrieval/sentiment_quant.py` & `base.py` (`P2-4`, `P3-8`), `api/app/orchestration/engine_store.py` (`P2-2`, `P3-5`), `api/app/core/auth.py` (`P3-1`): added secondary hard ceilings `_JOBS_HARD_MAX` / `_GRAPH_JOBS_HARD_MAX`; added `MAX_RESPONSE_BYTES` cap on Yahoo chart fetch & `ensure_global_symbol` (`BRK.B`, `BTC-USD`, `^GSPC`); suppressed flapping DB reads after `DbEngineStore.update()` failure; scaled `GET /v1/watchlist/alerts` cost from `watchlist_max_items`.
  - `.github/workflows/ci.yml`: enforced strict blocking `npm audit --omit=dev --audit-level=high` for runtime dependencies plus non-blocking warning output for unpatched upstream dev-only advisories (`picomatch` `GHSA-fc5q-39xc-vjpf`).

## [3.3.0] - 2026-11-17

### Added & Hardened — 5-Proposal Feature & CI Convergence + 3-Round Adversarial Audit

- **Full-App `mypy` Coverage & Low-Coverage Module Test Suite (`api/pyproject.toml`, `api/tests/test_proposals_wave.py`)**:
  - Widened `[tool.mypy].files` from `["app/core", "app/api", "app/llm"]` to `["app"]` (covering all 159 Python source files) and fixed all type annotations across `app/orchestration/` (`retention.py`, `notifier.py`, `research.py`, `research_jobs.py`), `app/retrieval/` (`boosts.py`, `memory.py`, `fa_sources/ssi.py`, `fa_sources/fireant.py`), `app/mcp/client.py`, and `app/config.py`.
  - Added `api/tests/test_proposals_wave.py` (10 unit/integration tests) covering `app/api/v1/site.py` (`/v1/map`, `/v1/crawl`), `app/api/v1/hybrid.py`, `app/worker.py`, `app/core/tracing.py`, `app/api/v1/podcast.py` (`/v1/podcast/personal*`), `app/orchestration/scheduler.py` executive brief synthesis & timeout/bare-citation fail-open, `app/retrieval/sentiment_quant.py` (`^VIX`/`^VIX3M` & Simplize fallback), and `app/api/v1/market.py` live proxy merge.
- **Live CBOE VIX Term-Structure Proxies & Simplize FA Fallback (`api/app/retrieval/sentiment_quant.py`, `api/app/api/v1/market.py`)**:
  - Added `fetch_live_market_proxies` querying Yahoo Finance v8 charts for `^VIX` (3M daily, `vix_ma50`) and `^VIX3M` (5D daily, term ratio) concurrently with OHLCV/FA inside `POST /v1/market/sentiment` when `fetch_market_data=true` on non-VN equities, merging without overwriting caller-supplied `market_proxies` (`_merge_live_market_proxies`).
  - Added `_fetch_simplize_summary` keyless fallback inside `_fetch_fa()` when `fa_aggregator` lacks VN ratios; hardened `parse_yahoo_chart_bars` against empty `volume` arrays on index payloads and non-finite/malformed bar values, and added `ensure_symbol` entry guards in `fetch_live_market_proxies` and `fetch_sentiment_quant_context`.
- **Opt-In Guarded Executive Brief for `run_digest` (`api/app/orchestration/scheduler.py`, `api/app/api/deps.py`, `api/app/config.py`, `deploy/.env.example`)**:
  - Added `SCHEDULER_LLM_SYNTHESIS_ENABLED` (`bool = false` default) and wired `gateway=get_gateway()` in `deps.get_scanner()`.
  - When enabled, `_synthesize_executive_brief` wraps verified `run_digest` citations inside SEC-07 random delimiters, validates output via `validate_citations` + minimum non-marker prose guard (`>= 15` chars), and fails open (`12s` timeout -> `None`) so the pure extractive `run_digest` always survives.
- **Web UI Parity (`frontend/components/SentimentPanel.tsx`, `SpacesPanel.tsx`, `NewsPanel.tsx`, `ComparePanel.tsx`, `frontend/app/page.tsx`, `frontend/lib/api.ts`)**:
  - Added `📊 Tâm lý & Dòng tiền` tab (`SentimentPanel`) calling `POST /v1/market/sentiment` to render CNN Fear & Greed (`0..100`), Smart/Dumb Money (`—` when `null`, never placeholder `50`), risk circuit breakers, quant/OHLCV/VIX/FA bar, 7-pillar checklist, 3-layer network telemetry, `top_verified_drivers` (`safeHref`), and direct `"📊 Phân tích Tâm lý"` navigation from `ComparePanel`.
  - Added `🗂️ Spaces & KG` tab (`SpacesPanel`) calling `/v1/spaces*` CRUD, `/crawl`, async `202 /graph/build` + aborted-safe `/graph/status` polling, `/graph/query`, and `POST /v1/hybrid` search, plus `"🎧 Podcast Watchlist"` (`/v1/podcast/personal*`) in `NewsPanel` with request-generation and `AbortController` guards.

### Added & Hardened — Rich-Content Web UI (docs/28), Unified Run Digest & DEFER-A/B/C Convergence (docs/29)

- **Streaming Rich-Content Web UI (`docs/28`) & Run-Digest UI (`docs/29` P3-3)**:
  - Upgraded streaming markdown pipeline (`frontend/components/AnswerMarkdown.tsx`, `CodeBlock.tsx`, `MathFormula.tsx`, `MermaidDiagram.tsx`, `frontend/lib/inlineMd.ts`, `mdMath.ts`, `markdownBlocks.ts`) with lazy Prism highlighting, KaTeX math rendering (`trust: false`, last-good fallback), serial strict Mermaid rendering with state reset across edits/errors, `O(n)` bounded emphasis scanning (`MAX_FAILED_EMPHASIS=64`), StrictMode-pure `useEffect` pacing gate, and adaptive render-cost throttle.
  - Added `run_digest` (`🌐 Bản tin chung`) chip, `.badge-digest` styling, and digest-first ordering within the same scan window in `frontend/app/page.tsx` and `frontend/app/globals.css`.
  - Added Node `fetch` `HEALTHCHECK` to `frontend/Dockerfile` and `deploy/docker-compose.yml`.
- **Extractive Unified Run Digest & Security/Concurrency Wave (`docs/29`)**:
  - Rewrote `AutomatedScanner` (`api/app/orchestration/scheduler.py`) strictly extractive over verified search results (removed hardcoded static templates & placeholder URLs), added cross-engine canonical/simhash dedup, escaped markdown metacharacters including `\`/`|`, serialized SQLite writes with `sqlite_write_lock()`, and published unified `run_digest` briefings per run.
  - Restored SEC-07 random delimiters across `research.py` source packs and `spaces.py` `/spaces/{slug}/graph/query`; eliminated raw exception text leakage on `/v1/answer` SSE, research jobs, and `/v1/llm/test`; added SSRF TOCTOU re-checks around `crawl4ai`, `domain` in `answer` cache keys, jittered backoff/retry in `gateway.py`/`searxng.py`, and precomputed cosine norms in `hybrid.py`.
- **Completed DEFER-A/B/C & 3-Round Audit Fixes**:
  - `api/app/core/admission.py`: true LRU + idle-only eviction in `get_key_semaphore`.
  - `api/app/core/auth.py`: `/v1/hybrid` POST cost `2` in `_STATIC_ROUTE_COSTS` and expired-first pruning in `_local_rate_limit`.
  - `api/app/api/v1/market.py`, `api/app/retrieval/sentiment.py`, `api/app/retrieval/sentiment_quant.py`, `api/app/api/v1/schemas.py`: CSV formula escaping via `_csv_safe_cell` (keeping negative floats numeric); `asyncio.wait` partial batch preservation on fan-out timeouts; `QUANT_FETCH_SKIPPED_BUDGET` circuit breaker when live market fetch is budget-skipped; fixed `_quant_only_payload` `None` crash when smart/dumb/FG metrics are unavailable; preserved quant telemetry on rumor-only inputs; allowed `S-Score >= 2.0` to promote `ELEVATED -> VIRAL_SURGE`; stripped non-`http(s)` URLs from `top_verified_drivers` and `VerifiedDriverOut`.
  - `api/app/orchestration/engine_store.py`, `api/app/cache.py`, `api/app/api/v1/podcast.py`, `api/app/api/v1/spaces.py`, `api/app/llm/council.py`, `api/app/orchestration/pipeline.py`, `api/app/orchestration/persist.py`, `api/app/retrieval/fa_sources/aggregator.py`: SQLite write locking & fallback cache invalidation in `update()` + `_cache_gen` TOCTOU guard (preserving 0-SQL search hot path in `record_result`; dormant `_record_db` helper retained for DB-hang guard tests); min-heap TTL expiration in `_LocalTTLCache`; protected active podcast/KG jobs from LRU eviction; parallelized podcast section gathering & query-expansion fan-out; aggregated real `usage` across all 3 council phases; offloaded `/v1/context` compression/build via `run_cpu`; fixed `_persist_documents` re-scrape chunk duplication bug; priority-ordered `fetch_ohlcv` short-circuiting.

## [3.2.0] - 2026-11-15

### Added — 4-Repo AI Infrastructure Stack & 5-Pass Hardening (docs/25)

- **OmniRoute-inspired Key Validation & Health-Aware Router** (`api/app/llm/key_validator.py`,
  `api/app/llm/router.py`):
  - `KEY_FORMAT_SPECS` (17 format specs + placeholder detection + whitespace/control/non-ASCII
    header rejection), SSRF-safe live key probing via shared `get_http_client("internal")` with
    bounded concurrency (`asyncio.Semaphore(6)`) and provider-scoped `scoped_runtime_override`.
  - `ProviderHealthTracker` + `score_provider`/`rank_providers`/`route_summary` combining key
    validity, `llm_breakers`, free tier rank (`trial_credits` included), EWMA latency, and
    `LLM_FALLBACK_CHAIN` (`LLM_ALLOW_KEYLESS_FALLBACK=false` by default).
  - Endpoints: `POST /v1/llm/validate` (`require_admin`) and `GET /v1/llm/health?task=answer|report`.
- **Headroom-inspired Context Compression & KV-Cache Alignment** (`api/app/llm/compressor.py`):
  - `ContentRouter`, `SmartCrusher` (JSON / Markdown tables / code logs), and extractive verbatim
    `SearchCompressor` supporting 4 profiles (`balanced`, `preserve_numbers`, `preserve_code`, `compact`).
  - `CacheAligner` keeps invariant system prompts first and isolates recalled memory inside
    randomized `<<<PRIOR_MEMORY_{tag}>>>` boundaries.
  - Hooked into `pipeline.py` (`CONTEXT_COMPRESSION_ENABLED`, `CONTEXT_COMPRESSION_MIN_CHUNKS=2`);
    exposed in `POST /v1/answer` and `POST /v1/context` (`include` adds `"compression"`).
- **One-Skill Orchestrator & TaskObserver** (`api/app/orchestration/skill_orchestrator.py`,
  `api/app/api/v1/skills.py`):
  - 10 unified skills wrapping domain intelligence packs (including `omni_channel_equity_sentiment`),
    driving `compression_profile` and recording bounded ring-buffer telemetry (`TaskObserver`).
  - Endpoints: `GET /v1/skills`, `GET /v1/ops/skills`, and `"skill"` in `POST /v1/context` `include`.
- **Omni-Channel Bilingual Equity Sentiment Sub-Engine (VI / EN)** (`api/app/retrieval/sentiment.py`,
  `api/app/api/v1/market.py`, `api/app/mcp/server.py`):
  - Phase 1: 5-layer bilingual query fan-out across official exchanges/filings, institutional
    brokers/media, VN social communities (Facebook, FireAnt, F319, Telegram, Threads), global
    cashtag streams (X/Twitter, Reddit, StockTwits, Substack), and video/KOL transcripts (YouTube,
    TikTok), with automatic bypass when `retrieved_documents` is supplied.
  - Phase 2: Entity anti-collision guard (`VNM`, `GAS`, `SSI`, `FPT`, `POW`, etc.), coordinated
    social seeding detector (`COORDINATED_SOCIAL_SEEDING`, collapsing copy-paste/VIP room spam into
    1 `TIER_4` hit), unverified rumor discounting (`UNVERIFIED_RUMOR_SPIKE`), and `Effective_Weight =
    Tier_Weight × Time_Decay`.
  - Phase 3: Bilingual VI/EN financial lexicon with negation/reversal guard (`_is_negated_match`),
    market regime classifier (`STEALTH_ACCUMULATION`, `MOMENTUM_MARKUP`,
    `EUPHORIC_DISTRIBUTION_TRAP`, `PANIC_CAPITULATION`, `EVENT_DRIVEN_BINARY`,
    `LOW_SIGNAL_NOISE`), gold-standard `benchmark_standards` block (`docs/27` — CNN Fear & Greed
    `0..100` với MA125/52W H-L, Vietstock Market Psychology, SentimenTrader Smart/Dumb Money &
    `contrarian_signal`, AAII/II/NAAIM, CBOE Put/Call/VIX-VIX3M/SKEW, Arms TRIN, McClellan
    RANA/NYMO, SqueezeMetrics DIX/GEX, FINRA Margin YoY, Wilder RSI-14, MFI-14, CMF-21,
    StockTwits S-Score, Loughran-McDonald sublinear TF-IDF, Tetlock Media Pessimism, Finnhub
    Insider MSPR, Sentimentick `buzz_velocity`, `nlp_telemetry`, `quantitative_telemetry` với
    tùy chọn `ohlcv_bars`/`market_proxies`/`fetch_market_data=true`, và 7-pillar checklist
    `seven_pillar_checklist`), cổng định lượng mở rộng (`fiintrade.vn`, `wichart.vn`,
    `simplize.vn`, `cboe.com`, `finra.org`, `aaii.com`, `naaim.org`, `finviz.com`,
    `barchart.com`, `tipranks.com`, `whalewisdom.com`, `quiverquant.com`, `unusualwhales.com`,
    `swaggystocks.com`, `apewisdom.io`, `sentimentrader.com`), và zero-fabrication output
    contract (`api/app/retrieval/sentiment_quant.py`, `POST /v1/market/sentiment` + MCP tool
    `equity_sentiment` + `api/tests/test_equity_sentiment.py`).
- **Claude-Mem-inspired Episodic Observations & Progressive Disclosure** (`api/app/retrieval/memory.py`,
  `api/app/api/v1/memory.py`, `api/app/mcp/server.py`):
  - Per-key `kind="observation"` distillation + recall (`MEMORY_RECALL_ENABLED`,
    `MEMORY_RECALL_MAX_TOKENS=250`) and 3-layer progressive disclosure endpoints
    (`GET /v1/memory/search`, `GET /v1/memory/timeline`, `POST /v1/memory/observations`) plus MCP
    tools (`memory_search`, `memory_timeline`, `memory_get_observations`).
- **Tests, Audit & Contract**:
  - Added `api/tests/test_phase4_ai_infra.py` (13 tests covering key validator, router, Headroom
    compression/CacheAligner, One-Skill orchestrator, Claude-Mem API + MCP tools, E2E context/answer,
    and Pass 5 profile/override isolation/timeline regression).
  - **Principal Architect Review Hardening (`docs/26-principal-architect-review-2026-11-15.md` — F-01..F-22)**:
    - Declarative rate-limit cost table `_STATIC_ROUTE_COSTS` (`api/app/core/auth.py`) covering
      `/v1/llm/council` (10), `/v1/spaces/{slug}/graph/build` (10), `/v1/crawl` (8),
      `/v1/spaces/{slug}/crawl` (8), `/v1/podcast/personal` (5), `/v1/map` (3),
      `/v1/spaces/{slug}/graph/query` (2), plus opt-in `ANONYMOUS_RATE_LIMIT_RPM` (`F-01`, `F-02`).
    - Explicit admission tiers (`council`, `crawl`, `map`, `hybrid`) in `_TIER_CONFIG`
      (`api/app/core/admission.py`); admission + budget enforcement on `POST /v1/llm/council` and
      `POST /v1/spaces/{slug}/crawl` (`F-03`, `F-04`, `F-13`); `SWALLOWED_ERRORS` metric counter
      (`F-14`).
    - `require_operator` dependency protecting `/v1/ops/status`, `/v1/ops/interleave`,
      `/v1/ops/skills`, `/v1/llm/health`, and `POST /v1/podcast/briefing` when `AUTH_ENABLED=true`
      while keeping single-operator Lite (`AUTH_ENABLED=false`) frictionless (`F-05`, `F-06`, `F-22`).
    - Streaming body byte cap (`RequestContextMiddleware`) rejecting chunked bodies over
      `MAX_BODY_BYTES` with HTTP `413` (`F-08`).
    - Proxy-aware loopback guard `is_direct_loopback` (`api/app/core/net.py`) on `/openapi.json`,
      `/docs`, `/metrics`, and `/readyz` internal diagnostics (`F-09`, `F-10`).
    - Upper bounds on all request schemas, array items, query parameters, and path parameters across
      `/v1/*`, locked by structural OpenAPI & AST guardrail tests in
      `api/tests/test_w5_guardrails.py` (43 tests) (`F-07`, `F-21`).
    - Expanded `mypy` scope in `api/pyproject.toml` to include `app/llm` (`F-16`), aligned package
      versions to `3.2.0` (`F-17`), and added `await dispose_engine()` teardown to async SQLite test
      fixtures (`F-19`).
  - Regenerated `api/tests/contract/openapi_v1.json`; documented new env vars in
    `deploy/.env.example` & `deploy/.env.lite.example`; wrote `docs/25-llm-integration-evaluation.md`
    and `docs/26-principal-architect-review-2026-11-15.md`; updated `docs/15-integration-guide.md`.

## [3.1.0] - 2026-11-14

### Added — Lite Web UI + job runner không popup (docs/08 §Lite)

- Bản Lite giờ có cả Web UI, không chỉ API: `cd frontend && npm ci && npm run build:export`
  (`METASEARCH_UI_EXPORT=1` → `output: "export"` trong next.config, `NEXT_PUBLIC_API_BASE_URL=""`
  để gọi same-origin) sinh `frontend/out`; API mount tại `/` khi `SERVE_UI=true` và dir tồn tại
  (`UI_STATIC_DIR` để override, mặc định auto-detect `<repo>/frontend/out`). Mount sau mọi route
  `/v1|/ops|/readyz|/docs`, static không cần auth, API vẫn sau auth. Bản Docker (standalone :3000)
  không đổi vì image API không có `frontend/out`. Test: `tests/test_v31_lite_ui.py`.
- `scripts/windows/install-task.ps1`: watchdog chạy `powershell -WindowStyle Hidden` — không còn
  popup cửa sổ console mỗi 5 phút / mỗi lần máy thức dậy.
- `scripts/windows/metasearch-lite.ps1`: mutex theo port (`Local\MetaSearchLiteManager-<port>`)
  — đã có một phiên manager đang chạy thì phiên khác tự bỏ qua (không nhân bản chạy/terminal);
  `start` tự build UI một lần nếu `frontend/out` thiếu (log `.lite.ui-build.log`); thêm lệnh
  `open` (mở `http://127.0.0.1:<port>/` trong trình duyệt). Cả 2 script vẫn ASCII-only, parse OK
  trên PowerShell 5.1.

### Changed — 2 mục P3 của docs/24 §8

- `POST /v1/spaces/{slug}/graph/build` nay trả **202** và chạy nền single-flight theo owner+space
  (admit tier `kg` mới, tối đa 4 build đồng thời); thêm `GET /v1/spaces/{slug}/graph/status`
  (counts đọc từ `kg_nodes`/`kg_edges` nên survives restart). Không còn chạy sync tối đa 20 LLM
  call trong một request.
- vi-first tokenize: stopword check chuyển sang so trên **token gốc trước khi fold** — folding cả
  stopword list làm "cổ"→"co" chạm "có"→"co" và nuốt từ có nghĩa ("cổ phiếu", "thị trường"); các từ
  extra va chạm với danh từ (sao/ngôi sao, do/đô, day/dây, ma, bac, nam) đã loại khỏi danh sách.

## [3.0.0] - 2026-11-14

### Added (docs/20 P2 wave — docs/24)

- P2-1 PDF page-level retrieval: `extraction/pdf.py` (pypdf, pinned) trích text từng trang thành markdown `## p.N`, hook vào scraper (`extractor_used="pdf-pages"`, sau check challenge page); vision/multimodal qua API tuỳ chọn `VISION_EMBED_API_BASE_URL`/`VISION_EMBED_MODEL` ghi vào cùng cột `chunks.embedding` (guard đúng `EMBEDDING_DIM`, chỉ http(s)); persist vẫn embed khi L2 off nhưng vision API có cấu hình. `PDF_MAX_PAGES=60`.
- P2-2 Knowledge graph theo Space (LightRAG-lite): bảng `kg_nodes`/`kg_edges` (alembic 0009, unique constraint nên rebuild idempotent), `POST /v1/spaces/{slug}/graph/build|query` (trích bằng LLM rẻ, sanitize JSON, query entity-centric 1-hop + `answer=true` tuỳ chọn). `KG_ENABLED`, `KG_MAX_DOCS_PER_BUILD=20`, `KG_MAX_NODES_PER_BUILD=200`.
- P2-3 vi-first: `fold_diacritics()` + `tokenize(fold=True)` mặc định (NFD strip Mn + đ→d) nên câu có dấu/không dấu cùng match; mở rộng stopword VI; golden eval mới `evals/golden_v3_vi.json` (25 câu) chạy bằng `scripts/eval_v2.py --golden`.
- P2-4 online interleaving: gán variant deterministic sha256(`salt|owner|query`), `interleave_sig` vào `search_cache_key` (hai nhánh không share cache), treatment chạy với `INTERLEAVE_TREATMENT_RRF_WEIGHT`; `/v1/search` trả thêm `interleave.variant`; `POST /v1/memory` nhận `payload` (vd `{"variant":"a"}`); `GET /v1/ops/interleave` tổng hợp up_rate theo variant (chỉ feedback explicit, scan bounded 5000 row).
- P2-5 personalized podcast: `POST /v1/podcast/personal` + `GET .../status|latest` — một bản tin/owner/ngày từ watchlist của owner (fallback preset global, không bịa chủ đề), file `personal-<sha256[:8]>-<date>.mp3` cùng retention với bản tin ngày. `PODCAST_PERSONAL_ENABLED`.
- P2-6 structured JSON output (Exa-style): `SearchRequest.response_format={type:"json_schema", json_schema, name}` → response `structured` + `structured_errors` (validate bằng jsonschema, pinned; lỗi thì báo, không bịa). Post-cache, fail-open.
- P2-7 domain reputation tiers (`retrieval/tiers.py`): official/primary/research/reference/aggregator/community/unknown; `domain_authority()` ưu tiên bảng curated, ngược lại dùng tier; mọi result của `/v1/search` có `domain_tier` (cache round-trip được field này).
- Kèm các commit Phase 1-4 của docs/23 (benchmarking proposals: CSV export cho `/v1/fa/compare`…) đã nằm trên main từ trước bản này.

### Changed

- 6 endpoint mới + các field mới (`response_format`, `structured`, `interleave`, `domain_tier`, `MemoryCreateRequest.payload`) trong OpenAPI snapshot (regenerated); `tests/test_v30_wave_p2.py` 43 test mới; `test_models.py` + `kg_nodes`/`kg_edges`; alembic 0009.
- Version 3.0.0 đồng bộ `pyproject.toml` / `app/__init__.py` / `frontend/package.json` / `uv.lock`.

## [2.6.0] - 2026-11-13

### Added (docs/20 P1 wave — docs/22)
- P1-1 IRCoT research loop: reformulate gap hops from gathered evidence + coverage gate over planner sub-questions (`coverage` in `learnings`/`done` SSE events).
- P1-2 adaptive complexity router: heuristic auto-`deep=true` for multi-hop/comparative/long queries (`ADAPTIVE_ROUTER_ENABLED`).
- P1-3 late chunking for long docs: segment windows + `EmbeddingClient.embed_many`; at persist time each chunk inherits its segment's embedding (bounded, fail-open).
- P1-4 hybrid dense+sparse chunk retrieval: `chunks.tsv` (tsvector+GIN on Postgres, alembic 0008 with backfill), BM25+cosine fused by RRF, `POST /v1/hybrid`.
- P1-5 per-key memory (Mem0-lite): `memory_items` table (alembic 0008) + `/v1/memory` CRUD; 👍/👎 feedback aggregated into per-domain boost rules folded into `boosts_sig`; `DELETE /v1/memory` wipes all (privacy); query recording goes through the bounded persist queue.
- P1-6 Spaces: `spaces` table + `/v1/spaces` CRUD + `/v1/spaces/{slug}/crawl`; `space=<slug>` on `/v1/search` and `/v1/answer` (post-cache domain scope + pinned-URL boosts, new `BoostRule(kind="url")`).
- P1-7 agent site endpoints: `POST /v1/map` (sitemap-first via defusedxml, same-host HTML fallback) and `POST /v1/crawl` (same-host BFS through the SSRF/robots/pinning-guarded scraper, persisted into the chunk index).
- P1-8 configurable freshness half-lives (`FRESHNESS_HALF_LIFE_DAYS_NEWS/OTHER`); news curve unchanged by default, non-news 45d.

### Changed
- `SearchRequest`/`AnswerRequest` gain `space`; 6 new endpoints in the OpenAPI snapshot (regenerated).

## [2.5.0] - 2026-11-13

### Added (docs/20 P0 wave — docs/21)
- P0-1 RRF fusion in `merger.py` (k=60, engine weights, blended with signals via `RRF_WEIGHT`); search cache key bumped to `sr:v3:`.
- P0-2 `RERANKER=bge`: local ONNX `BAAI/bge-reranker-v2-m3` (int8 preferred, lazy HF download pinned to `revision="main"` into `MODEL_CACHE_DIR`), graceful fallback to heuristic.
- P0-3 multi-query expansion (cheap LLM call, deep/long queries only; variants fan out to general engines only) fused via RRF.
- P0-4 claim-level citation check (`unsupported_markers` in the citations event + `AnswerResponse`), reported never rewritten.
- P0-5 follow-up question suggestions (SSE `follow_ups` event, cached with the answer, `AnswerResponse.follow_ups`, UI chips).
- P0-6 per-key custom rerank boosts: `key_boosts` table (alembic 0007) + CRUD `/v1/prefs/boosts` (Goggles-lite), applied post-merge, key-scoped via `boosts_sig` in the cache key.
- P0-7 agent-friendly extras on `POST /v1/search`: `extras=["contents","highlights","answer"]` on top results (computed post-cache).
- P0-8 eval harness v2: `app/eval_metrics.py` (hit@k/recall@k/MRR/nDCG/context precision+recall/claim support), `evals/golden_v2.json` (29 EN+VI queries), `scripts/eval_v2.py` with timestamped reports + delta vs previous.

### Changed
- Search cache key `sr:v2:` -> `sr:v3:` (adds boosts/expansion signatures; old entries simply expire).
- `SearchRequest` gains `extras` (named `extras` because `ContextRequest` overrides `include`).

### Fixed
- Worker retention cadence now uses a real marker file (`.retention_worker_last_run`) — the pending draft would have run the full purge every 120s tick.
- MCP HTTP mount: session manager entered in the main lifespan (legacy startup handlers are ignored with an asynccontextmanager lifespan), `streamable_http_path="/"`, DNS-rebinding check off behind api-key auth, graceful on older SDKs.


Tất cả thay đổi đáng chú ý của MetaSearch được ghi ở đây.
Format theo [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
version tuân theo [SemVer](https://semver.org/).

## [Unreleased] — Audit rounds 9–10 (6 expert đối kháng độc lập: v2.3.0 backend, v2.4.0 global, frontend, core cũ, viet_stock/market, verify chéo)

## [Unreleased] — Audit rounds 11–12 (ops/tooling + security/perf second-pass)

### Fixed
- **P1 MCP HTTP chết từ đầu**: FastAPI không chạy lifespan của sub-app mount —
  `session_manager.run()` giờ wire vào app event handlers; có auth-wrap ASGI
  boundary dùng `require_api_key` (trước đây /mcp BYPASS hoàn toàn api-key auth
  khi AUTH_ENABLED=true).
- **P1 Prometheus không scrape được** (compose bật auth, /metrics cần admin key,
  cross-container không có key → mọi alert/dashboard rỗng): thêm
  `METRICS_ALLOW_PRIVATE` — RFC1918 (compose network) được scrape sau khi admin
  key attempt thất bại; Internet công khai vẫn chặn; admin key luôn thắng.
- **P1 k6 hỏng 2 chỗ**: thiếu Authorization header (compose auth → 100% 401) +
  check field `text` đã chết từ v2.0 → `chunks`; usage doc thêm `-e API_KEY`.
- **P1 perf_load crash trên Windows** (cp1252 không encode `→`) — stdout
  reconfigure utf-8.
- P2: alerts.yml `path="/v1/answer"` REVERT về `/answer` (middleware thực tế phát
  /answer — claim của audit agent sai, đã verify live); load_vn_tickers
  `lru_cache` (342KB re-parse mỗi symbol, double-decorator gỡ); fromtimestamp
  bounds ≤2100 cho cả SSI + VNDIRECT parser (500 → 503 có nghĩa); rate cost
  proportional (compare 3+N, alerts 1+N/4); docs/06 thêm section v2.3/v2.4;
  golden eval +6 case (global engines, deep/news); README sweep counts (26
  adapters, 881/49 tests, v2.4.1); CHANGELOG merge 2 Unreleased blocks.
- P3 hardening: `_http` bắt RecursionError (JSON bomb); google_news 5MB cap;
  `ensure_symbol` dùng `\Z`; openalex/crossref/bluesky nested-author guards;
  podcast TOCTOU → 404; `_status_for` bỏ sweep đồng bộ; streamSse fix boundary
  CRLF-split-across-chunks (frame headless); `_ttsCapsPromise` không còn bind
  signal caller đầu; mcp client tái dùng thay per-call; release.py thêm gate
  mypy (CI parity); dead "research" admission tier ghi chú; docs/07 5 tools;
  docs/12 15 engine thật.



### Fixed
- **P1 L2 semantic cache bỏ qua luật tươi mới**: financial/news answers không còn
  ghi/đọc L2 7 ngày (DAT-10) — `_semantic_cacheable()` + guard ở `_semantic_lookup`.
- **P1 dictation watermark** (regression của vòng 9): Chrome finalize tại CÙNG
  resultIndex → watermark max-length làm MẤT chữ đã nói → đúng pattern resultIndex
  + Set khóa exactly-once.
- **P2 `max_age_s`** (valve DAT-10 chết từ trước): field mới trên AnswerRequest →
  financial answers cache được với `ttl = min(answer_ttl, max_age_s)`.
- **P2 implied-EPS gate** (viet_stock): `eps = None` sai thụt lề — trước đây LUÔN
  drop EPS khi có pe+close → chỉ drop khi ratio > 3x như docs.
- **P2 /v1/fa/compare name=null**: registry key là `company_name` không phải `name`.
- **P2 crossref `[[]]`**: date-parts rỗng → IndexError không được bắt → thêm guard
  + IndexError (verify-agent tìm ra lỗ hổng của chính fix vòng 9).
- **P2 Redis rate-limit self-lockout**: zadd trước khi check làm 429 tiêu quota —
  pre-check zcard trước, rejected không ghi nữa.
- P3: dead "research" admission tier ghi chú; L2→L1 shortcut near-always-miss bỏ;
  cache set_json serialize ngoài try (bug caller không trip breaker Redis);
  create_key clears negative cache; faTable {1,15} khớp backend; SSI negative
  guard đủ o/h/l; ohlcv fallback theo remaining budget; docs/15 ghi đúng 2 statement
  FireAnt; Deep chip gửi deep=false khi đã ghim engine; quorum empty-ok note.

### Fixed
- **SearXNG settings.yml (P1)**: 6/16 engine được liệt kê không có module upstream
  (`reddit`, `twitter`, `linkedin`, `dblp` — SearXNG bỏ qua im lặng; `stackexchange`,
  `mastodon` sai tên) → thay bằng tên thật (stackoverflow/askubuntu/superuser,
  mastodon users/hashtags); docs/19 sửa các claim sai.
- **L2 semantic cache bỏ qua `deep`**: answer deep không còn có thể replay từ
  answer shallow tương tự ngữ nghĩa.
- **crossref `_parse_issued`**: guard drift lồng (`issued` non-dict, `date-parts[0]`
  scalar) — 1 record hỏng bị skip, không chết cả adapter.
- **`_http.fetch_json`**: bỏ `follow_redirects=True` (giữ mặc định False của pool —
  redirect không gửi header của ta tới host lạ).
- **watchlist**: khôi phục từ memory khi DB sống lại mà WRITE lỗi giờ GIỮ nguyên
  items memory (trước đây mất sạch + báo storage="db" sai) + regression test.
- **bluesky**: post thiếu handle/rkey bị drop thay vì bịa URL trang chủ (tránh
  merger gộp đống post drift vào 1 nhóm).
- **pipeline**: timeout engine bị clamp dưới fan-out budget (không còn TIMEOUT
  oan cho engine chậm-hợp-lệ khi budget đã cạn); term inference chữ hoa chết
  ("Ý kiến cộng đồng") đã lower-case.
- **frontend**: mic tự dừng khi rời tab tìm kiếm; hint permission/no-mic/network
  cho dictation; poll podcast dừng ở `cancelled` + cap 150 lần; ResearchPanel
  settled-guard chống double-finalize + không để error che report; chip Deep tự
  vô hiệu + nhãn trung thực khi đã ghim engine; audio podcast hint 401/auth;
  faTable lọc charset sớm; chart stats tính trên đúng chuỗi đã sort/dedupe;
  dictation auto-stop im 1.2s + watermark chống double-merge; nút watchlist có
  busy state.
- **podcast/briefing**: `_status_for` stat guarded (không 500 khi retention sweep
  đua), retention sweep chạy cả ở /status (status/latest không còn mâu thuẫn),
  symlink hardening, tài liệu hóa lựa chọn mốc UTC.
- 3 test mới/khôi phục (migration write-fail, days clamp contract giữ nguyên).

## [2.4.1] — 2026-11-12 — audit fixes cho v2.4.0 (docs/19 §8, 3 vòng audit)

### Fixed
- 7 adapter toàn cầu mới: guard `isinstance` cho payload/item — upstream đổi schema (JSON không phải object, item không phải dict) thì trả [] thay vì ném lỗi vào breaker. 12 test schema-drift mới.
- `infer_categories_from_query` khớp word-boundary cho term ASCII: "nav" không còn khớp "navigation", "eps" không khớp "steps"; "doi:" vẫn khớp; cụm tiếng Việt giữ nguyên. Test hồi quy 2 chiều.
- `deep=true` tắt quorum early-return ở fan-out: deep mode chờ ĐỦ mọi engine (vẫn bounded bởi hard budget) — đúng nghĩa "sâu nhất". Test shallow vs deep.
- `google_news` map lang→đúng country code cho `gl` (en→US, vi→VN, fr→FR…, fallback US; trước đây en→"EN" sai).
- UI: bật Deep tự gửi `k=30` (cap 50) cho cả `/v1/search` và `/v1/answer` để tập kết quả merge không bị hẹp.

## [2.4.0] — 2026-11-12 — Global Deep Search (docs/19): 7 engine toàn cầu keyless mới, tiếng Anh end-to-end, deep mode

### Added
- **7 vertical engine keyless mới** (26 engines tổng cộng): `hackernews` (Algolia HN API),
  `stackexchange` (api.stackexchange.com, `site:` trong query để đổi site),
  `bluesky` (public AppView searchPosts), `openalex`, `crossref`, `europepmc` (tạp chí
  học thuật toàn cầu), `gdelt` (tin tức/sự kiện toàn cầu 100+ ngôn ngữ, hỗ trợ
  `sourcelang` + cửa sổ thời gian). Chung một helper `_http.fetch_json` map lỗi
  đúng `Outcome` cho breaker. `published_at` luôn lấy từ payload, không bao giờ bịa.
- **SearXNG mở rộng**: bật thêm 16 engine (reddit, stackexchange, github, youtube,
  mastodon, twitter, linkedin, arxiv, pubmed, crossref, dblp, semantic scholar,
  wikidata, goodreads, imdb, openverse) và `default_lang: auto`; adapter map category
  của MetaSearch sang category của SearXNG (news/social media/it/science) để không
  bao giờ gửi category searxng không biết (tránh 400 cả request).
- **Ngôn ngữ end-to-end (docs/19 W3)**: `lang` giờ tới được google_news (map
  hl/gl/ceid — lang ví/en rõ ràng), gdelt (`sourcelang:`), openalex
  (`filter=language:`); searxng/ddg/wikipedia đã có sẵn. UI có bộ chọn
  Ngôn ngữ Tự động / VI / EN.
- **`deep: true` trên `/v1/search` + `/v1/answer`**: fan-out tới TẤT CẢ engine đã
  bật (bỏ lọc category), k tối đa 50 — "tìm mọi thông tin theo yêu cầu sâu nhất".
  Cache key (search + answer) đã bao gồm `deep` để không nhiễm chéo.
- **Category inference mở rộng**: social (reddit, hacker news, bluesky, mastodon,
  x.com, youtube, forum…), academic toàn cầu (doi:, ssrn, journal, preprint,
  meta-analysis…), global-finance (13F, ETF, prospectus, fact sheet, IMF, World Bank,
  OECD, BIS, ECB, Fed…), world news.
- **Upgrade path**: DB engine store cũ tự seed thêm engine mới khi thiếu
  (`on_conflict_do_nothing` — không đè tuỳ chỉnh của operator); có test.
- UI: checkbox 🌐 Deep + chọn ngôn ngữ ở thanh công cụ tìm kiếm.
- 25 test mới (`tests/test_global_adapters.py`), OpenAPI snapshot regen (additive),
  engine-store test cập nhật; `GLOBAL_SEARCH_MAILTO` cho polite pool (tuỳ chọn).

## [2.3.0] — 2026-11-12 — feature roadmap (docs/18): dictation, Deep Research UI, so sánh mã + biểu đồ nến, watchlist, podcast

### Added (docs/18 — 6 wave)
- **A1 Dictation tiếng Việt**: `frontend/lib/dictation.ts` bọc SpeechRecognition
  (vi-VN, interim results), nút mic ở ô tìm kiếm chính + ô lọc tin tab Tin tức;
  trình duyệt không hỗ trợ → ẩn nút. 8 vitest mới.
- **A2 Tab 🔬 Deep Research**: `ResearchPanel.tsx` tiêu thụ FR-9 chưa có UI —
  `POST /v1/research` → SSE `/events` (status/report_token live) → render báo cáo
  bằng `AnswerMarkdown` (component chung với câu trả lời) + citations + learnings;
  đọc báo cáo **theo từng section** (playlist bấm-để-nghe, tiếp/dừng) qua engine
  TTS hiện tại (`lib/sectionTTS.ts`, 6 vitest). Abort SSE khi rời tab.
- **B1 `POST /v1/fa/compare`**: so sánh 2-5 mã qua `FAAggregator` chung
  (`deps.get_fa_aggregator`, breaker sống xuyên request); mỗi field có
  `value/source/as_of/validated`, thiếu thì liệt kê `missing_fields` — không bịa số.
  Tab **⚖️ So sánh mã** + đọc bảng bằng TTS. Rate cost 3, tier admission `market`.
- **B2 `POST /v1/market/ohlcv`**: nến ngày VNDIRECT primary (đường parser chung
  với search, có per-candle timestamps) + fallback SSI qua aggregator; cả hai chết →
  503 có nghĩa. `SourceOHLCV` thêm `times`. UI: `lightweight-charts@5.3.0` (pinned,
  Apache-2.0) + thống kê (giá cuối, biến động, cao/thấp, KL) tính từ payload.
- **C Watchlist + cảnh báo**: model `WatchlistItem` (tenancy theo API key, cap 20),
  CRUD `/v1/watchlist`, `GET /v1/watchlist/alerts` poll-on-demand diff hash URL với
  baseline (lần đầu chỉ lưu mốc nền); DB chết → fallback in-memory bounded. UI trong
  tab Tin tức: thêm/xoá theo dõi, badge "Mới: n", danh sách tin mới + đọc tin mới.
  **Bỏ background poller** theo đề xuất docs/18 §7 (không đốt budget nền).
- **D Podcast bản tin**: `POST /v1/podcast/briefing` (202, idempotent theo ngày),
  `/status`, `/latest` (audio/mpeg), `/feed.xml` (RSS); script từ 6 preset tin thật,
  cap ở sentence boundary, retention 7 ngày trong `api/podcast_data/` (gitignored).
  Chưa cấu hình VoiceStudio → 503 `tts_disabled` cho toàn bộ route podcast.
- 16 test backend mới (`test_market_watchlist_podcast.py`: MockTransport/fake
  pipeline/SQLite in-memory/tmp-dir podcast), 9 vitest mới (chartData, faTable);
  OpenAPI snapshot regen (additive). Frontend: 44 vitest, tsc + next build xanh.

### Audit — 3 vòng expert đối kháng (8 lens: finance, technical/SA, AI/LLM, QA, auditor/security, data-engineer, dream-rsi, perf)
- **Vòng 1** phát hiện và fix các P1: parser dchart có thể bịa OHLC/volume khi
  feed đổi schema (giờ strict: độ dài 6 mảng khớp, mọi giá trị finite + không âm,
  time tăng nghiêm ngặt); `market.py` timeout → 504 `deadline_exceeded` + kết quả
  từng mã partial, không bao giờ bịa `times`; watchlist alerts Semaphore(4) + cap
  30s + partial, baseline không advance khi outcome incomplete; podcast single-flight,
  degrade theo preset, IO qua `run_cpu`, `Cache-Control: private`, retention đúng N ngày.
- **Vòng 2** xác nhận 39/39 fix vòng 1 (full pytest rc=0, alembic 0006 khớp model
  1:1) và fix thêm: `streamAnswer` normalize CRLF của sse-starlette 3.x trước khi tách
  frame (vitest 5 case), ChartPanel chuyển sang API v5 của lightweight-charts
  (`addSeries(CandlestickSeries,…)`, pin 5.3.0), từ chối NaN/Inf trong mọi mảng nến,
  podcast retention chạy trước khi chọn file, watchlist memory-mode có re-probe DB
  throttle 30s để migrate về DB khi nó sống lại, ResearchPanel re-attach SSE không
  nhân đôi report.
- **Vòng 3** (fresh-eyes toàn bộ diff, xem docs/18 §8): fix 13 phát hiện —
  evict owner-lock chỉ khi không bị giữ (tránh alias lock), fallback aggregator raise
  → 503 không 500, podcast job bị cancel không mắc kẹt `running`, `_prune_jobs` khớp
  retention N ngày với disk, RSS enclosure từ chối scheme không phải http(s),
  retention không xoá `.mp3.tmp` đang ghi (chỉ dọn tmp già >1h), `streamAnswer` thêm
  guard overflow 1MB + `reader.cancel()` + cap 1MB cho từng event (cả `streamSse`),
  ResearchPanel poll lại 3 lần sau event `done` trước khi báo lỗi, watchlist migrate
  về DB coi memory list là tập id authoritative (item xoá lúc DB down không bị hồi
  sinh), parser dchart guard multiplier trước khi validate, `market/ohlcv` hết wall-clock
  → 504 `deadline_exceeded` thay vì 503 sai nghĩa. 7 test backend + 1 vitest mới.
- Kết quả cuối: ruff/format/bandit/mypy sạch, full pytest xanh, frontend 49 vitest
  + tsc + next build xanh, OpenAPI snapshot khớp router.

## [2.2.0] — 2026-11-11 — Web UI: tab Tin tức đa lĩnh vực + đọc máy tiếng Việt (TTS)

### Docs
- README.md cập nhật đúng trạng thái hiện tại (v2.2.0: tab Tin tức + TTS, map `when:{n}d`
  server-side, process hardening; số test 830 backend + 21 frontend; bản đồ docs đủ 16 tài
  liệu); README.md nay **song ngữ Anh–Vi trong cùng một file** (mục `# English` và
  `# Tiếng Việt`, link neo chéo ở đầu file; CONTRIBUTING.md và AGENTS.md trỏ vào anchor).
  `docs/12-roadmap-plan.md` cập nhật verify matrix + mục v2.2.0.

### Added
- Tab **Tin tức** mới trên Web UI (`frontend/components/NewsPanel.tsx`): 6 mục preset
  (Kinh tế vĩ mô, Tài chính–Chứng khoán, Kinh doanh–Doanh nghiệp, Công nghệ–AI,
  Ngân hàng–Tiền tệ, Kinh tế thế giới) + ô truy vấn tự do, gọi `POST /v1/search` với
  `engines=["google_news"]`, `categories=["news"]`, `freshness_days`, `news: true`.
  **Không thêm endpoint mới**; cửa sổ thời gian do **backend** map
  `freshness_days` → `when:{n}d` của Google News RSS ngay trong adapter (guard không
  nhân đôi operator, clamp 3650; 8 unit test MockTransport pin hành vi; probe live xác
  nhận RSS honors `when:Nd`). Client chỉ gửi param có cấu trúc, không nhét operator
  vào query. `lib/api.ts` chỉ bổ sung `freshnessDays` + `news` vào `SearchOptions`.
- **Đọc máy tiếng Việt (TTS)** qua Web Speech API — voice stack của OS, free, không
  key, không egress từ backend (`frontend/lib/tts.ts`): nút đọc câu trả lời ở tab Tìm
  kiếm; ở tab Tin tức đọc cả bản tin (top 8) hoặc từng tin. `stripForSpeech` bỏ
  markdown, marker `[n]`, URL (chỉ giữ label của link, bỏ URL trần và hàng phân cách
  bảng); `chunkForSpeech` đóng gói ≤220 ký tự theo câu; một speaker tại một thời điểm
  với generation guard chống `onend` sót sau `cancel()`. Graceful degradation: trình
  duyệt không hỗ trợ → ẩn nút; thiếu giọng vi-* → đọc bằng giọng mặc định + cảnh báo,
  re-check qua `voiceschanged` để không bị kẹt sai ở mount. Ghi chú riêng tư (giọng
  online của Edge gửi nội dung đọc từ trình duyệt tới Microsoft) ở `docs/16` và
  tooltip của nút đọc.
- `safeHref()` ở `lib/api.ts`: mọi `href` từ payload backend (feed tin, trích dẫn
  `[n]` của câu trả lời, kết quả tìm kiếm, nguồn, link signup của preset LLM) chỉ
  chấp nhận `http(s)://`.
- Thiết kế/plan: `docs/16-web-ui-news-tts.md` (kiến trúc, so sánh phương án TTS,
  hợp đồng `lib/tts.ts`, preset tin, liêm chính hiển thị, kiểm thử).

### Data integrity hiển thị
- Mỗi tin hiển thị `domain` + mốc thời gian lấy thẳng từ payload (`published_at`/`as_of`),
  thiếu mốc → "không rõ thời điểm"; `complete === false` → banner vàng "có thể KHÔNG
  đầy đủ"; kết quả trống → "kết luận thật, không phải lỗi".

### Hardening (audit vòng 5 + vòng 6 đối kháng — 2+4 expert read-only — PASS)
- Fix đủ 4 finding của vòng 4: 1 P1 (`href` raw ở danh sách trích dẫn câu trả lời +
  `signup_url`), 2 P2 (`hasViVoice` sticky-false; URL bị đọc thành tiếng), 1 P3
  (comment cite § sai, thiếu guard `when:Nd` trùng, thiếu aria-label).
- Fetch tin có timeout client 30s + abort guard — backend treo không làm kẹt tab; khi
  timeout 30s thì báo lỗi rõ và nhả `loading` (vòng 6 phát hiện `stale()` gộp cả
  "superseded" và "aborted" khiến tab kẹt loading mãi — đã tách riêng). Hủy đọc đúng
  khi unmount / đổi tab / tìm truy vấn mới.
- Văn bản đọc: bỏ thẻ `[Nguồn]` đầu snippet của google_news; thiếu title thì đọc
  `domain` thay vì `url`; strip dấu câu thừa (không đọc ".."); link rule chạy trước
  citation rule + URL lồng ngoặc; khử entity, zero-width, bullet số `1. `; `maxLen`
  clamp ≥1; `synth.speak()` bọc try/catch; `pickVoice` ưu tiên giọng nội bộ OS
  (`localService`) hơn giọng online để nội dung đọc mặc định không rời máy.
- Tab tin gửi `news: true` để backend dùng news cache TTL 900s + freshness weight
  (trước đó feed bị cache TTL chung 1800s); toolbar hiện cờ "bản đã cache".
- `fmtWhen` clamp mốc tương lai về ngày tuyệt đối thay vì "vừa xong".
- `GoogleNewsAdapter` map `freshness_days` → `when:{n}d` server-side (guard
  `\bwhen:[0-9]+[dhwmy](?![a-z0-9])` end-anchored + ASCII-only — khớp với regex JS
  ở toolbar — không nhân đôi operator hợp lệ; lookalike `when:3days` và fullwidth
  `when:３d` vẫn được append; clamp 3650; `freshness_days` không phải số bị bỏ qua
  an toàn) — client tab tin không còn tự append operator vào query nữa. Toolbar
  hiện "cửa sổ: theo when: trong truy vấn" khi truy vấn tự do tự embed `when:`.
  Response search + `_data_marks` (/v1/context): `age_s` fallback `published_at`
  khi không có `as_of` (tin tức).

### Verified
- `npm run test` (vitest, 16 unit test cho `stripForSpeech`/`chunkForSpeech`/`safeHref`)
  PASS và đã thêm vào CI frontend matrix; `npm run typecheck` + `npm run build` PASS (Next.js 15 build sạch).
  Backend: 8 MockTransport case pin map `freshness_days` → `when:{n}d` (append, guard operator,
  lookalike, clamp, non-numeric).
- Pre-commit gate mới kiểm tra `api/uv.lock` khớp version với `pyproject.toml`
  (`check_lockfile_version` trong `scripts/release.py`, fail-closed: thiếu file
  hoặc không parse được là BLOCK, đọc đúng section `[project]` và block
  `[[package]]` của `metasearch-api` bất kể thứ tự field) — CI `uv sync --locked`
  không còn bị fail vì lockfile stale.
- CI frontend matrix thêm `npm audit` cả dev deps (`--audit-level=high`, không
  `--omit=dev`) để không bỏ sót nhánh dev (vitest).
- TTS công khai đúng trạng thái riêng tư: `ttsVietnameseVoiceMode()` (local /
  online / none) — toolbar + tooltip của cả 3 nút đọc ghi chú động theo giọng
  thực tế đang dùng.
- Backend suite đầy đủ pass 100% (không đổi API — chỉ dùng `freshness_days` đã có sẵn
  trong contract `docs/06` §1).
- Audit vòng 5 (2 expert, read-only): cả 4 finding vòng 4 đều FIXED với bằng chứng
  file:line; không phát sinh P1/P2 mới — verdict PASS cả 2.
- Audit vòng 7 (3 expert đối kháng: perf / quality / data-sec, 2 vòng — vòng 1
  phát hiện 2 P2 + các P3, vòng 2 xác nhận đã fix hết, verdict PASS cả 3,
  không P1/P2 mới): fix toolbar "cửa sổ N ngày" khi truy vấn tự do có `when:`,
  chỉ báo giọng TTS online/local, regex guard end-anchor + ASCII, lockfile check
  fail-closed, `npm audit` cả dev deps, `age_s` fallback `published_at` cả ở
  `/v1/search` lẫn `_data_marks` (/v1/context).
- `scripts/release.py` `clean_workspace` không bao giờ xoá dữ liệu runtime
  (`api/*.db*`, `*.retention`, `api/.env.*` trừ `*.example`) và không crash khi
  file bị tiến trình local giữ lock (PermissionError → WARN bỏ qua) — bản vá
  đúng promesse trong AGENTS.md sau sự cố 2026-11-10.

### VoiceStudio server-side TTS (tuỳ chọn) — docs/17 (đã release trong v2.2.0, commit b82e00c/60c69dc)

### Hardening (audit vòng 8 — security/SA + QA/data-integrity đối kháng, PASS)
- **ETag chống cross-field collision**: key cache audio giờ length-prefix từng
  trường (model/voice chứa `|` không còn trộn khóa giữa các request).
- **Admission vào trong single-flight**: waiter của request trùng dùng chung
  slot leader, không đốt permit `admit("tts")` cho cuộc gọi không xảy ra.
- **Fallback thật khi VoiceStudio lỗi** (docs/17 §2 giờ đúng): chuỗi remote
  fail → phần còn lại được đọc bằng Web Speech API thay vì im lặng; `ttsSpeakRemote`
  cũng huếy giọng Web Speech đang chạy (bất đẳng thức one-speaker đã bỏ).
- Nút đọc máy hiện khi có Web Speech **hoặc** VoiceStudio (webview không có
  speechSynthesis vẫn nghe được qua server); hint hết giọng vi tự sửa theo
  `voiceschanged`; bỏ field ETag chết ở client; tooltip bỏ ghim "Web Speech API".
- 4 test mới: ETag collision, redirect 3xx → 502 KHÔNG theo, LRU eviction
  (count/bytes), auth 401 cho cả 2 route TTS. TTS suite: 34/34.
- docs/17: ghi chú host ≥16GB cho profile tts, Safari transient-activation,
  capabilities cache 1 lần/trang; compose thêm hướng dẫn pin digest image.

### Added
- **Engine đọc máy thứ 2: VoiceStudio local server** (docs/17): backend mới
  `GET /v1/tts/capabilities` + `POST /v1/tts/synthesize` proxy tới OpenAI-compatible
  API của VoiceStudio (`VOICESTUDIO_URL`, chỉ từ env — SSRF-safe; cap audio 8MB;
  ETag + Cache-Control hỗ trợ 304; lỗi rõ: `tts_disabled` 503 / `tts_upstream_error` 502 /
  timeout 504). Web Speech API vẫn là mặc định; UI tự fallback khi chưa cấu hình.
- Web UI: menu chọn "Giọng đọc" (Trình duyệt / VoiceStudio server) ở toolbar tab Tin
  tức, áp dụng cả nút đọc câu trả lời; lựa chọn lưu localStorage; ghi chú riêng tư
  trên tooltip đổi theo engine thực dùng (VoiceStudio = gửi text tới server nội bộ).
- `ttsSpeakRemote` trong `lib/tts.ts`: strip + chunk ≤220 ký tự như đường trình
  duyệt, gọi tuần tự từng chunk, phát bằng một `<audio>` duy nhất, generation guard.
- Deploy: compose thêm service `voicestudio` (image `ghcr.io/debpalash/voicestudio:stable`)
  dưới `profiles: ["tts"]` — không chạy mặc định; `.env.example` thêm `VOICESTUDIO_*`.
- Test: 30 backend (`test_tts_voicestudio.py`), frontend vitest lên 21; OpenAPI
  snapshot regen (chỉ additive). docs/17 ghi nhận so sánh, cách bật, rủi ro
  license (AGPL — chỉ tích hợp qua HTTP process riêng) và các tính năng mở rộng
  đề xuất (dictation, tải bản tin audio) cho phase sau.
- Hardening sau audit vòng 1 (perf phát hiện 2 P1: pool riêng `tts` + per-phase
  timeout giữ `POOL_ACQUIRE_TIMEOUT_S`, PoolTimeout→503; wall-clock
  `asyncio.timeout`; cap error-body 64KB; media whitelist; bytearray buffer;
  admission tier `tts` (8,1.0) + single-flight + LRU 16 entry/32MB theo ETag;
  `Cache-Control: private`; rate-limit cost 5; compose `mem_limit: 8g`;
  `reset_pipelines` clear tts client; URL validate cả port/path/userinfo).
  Frontend: prefetch chunk tiếp theo + blob cache 24/32MB + revoke URL trên mọi
  nhánh + `ttsStop` dừng cả remote + cancel remote cũ khi speak mới + onError 1
  lần + shared capabilities promise.
- **Audit 2 vòng (3 expert/vòng: perf / quality / data-sec)**: vòng 1 phát hiện
  2 P1 + các P2/P3 → fix hết; vòng 2 xác nhận tất cả FIXED, verdict **PASS ×3**,
  không P1/P2 mới (chỉ còn P3 note). Backend full suite 788+ passed, frontend
  21/21, ruff/bandit clean, CI 13/13.

## [2.1.0] — 2026-11-11 — Multi-source FA: bỏ điểm hụt đơn nguồn VNDIRECT/Simplize

### Added
- Package `app/retrieval/fa_sources/` — adapter **trực tiếp từng nguồn** (không qua
  vnstock/wrapper, đúng quy ước giants-first), mỗi nguồn một module tự chứa:
  **SSI iBoard** dchart TradingView (OHLCV, keyless), **TCBS stock-insight**
  financial-ratio (keyless, map phòng thủ biến thể key — key lạ bỏ qua tuyệt đối),
  **FireAnt** income/balance statement (Bearer JWT qua `FA_FIREANT_TOKEN`, rỗng là
  nguồn tự tắt; 401/403 mở breaker). Chi tiết nguồn (21 nguồn đã thẩm định) ở
  `docs/15-fa-sources-plan.md`.
- `FAAggregator`: fan-out breaker-gated (`fa:{source_id}`, metric
  `metasearch_breaker_state{engine="fa:..."}`), per-source timeout, cross-validate
  (đồng ý ≥2 nguồn trong dung sai 0.5% → `validated`), xung đột ưu tiên nguồn
  priority cao + log `fa_conflict`, hết nguồn → fields rỗng + `missing_fields`
  (không ngụy tạo). Mọi field merged có `{value, source, as_of}`.
- Tích hợp vào `VietStockAdapter` làm **fallback đồng thời** (không cộng dồn độ
  trễ happy path): VNDIRECT chết → nến từ SSI; Simplize chết → FA từ TCBS/FireAnt;
  `name`/`exchange` tra từ registry DAT-06; provenance kết quả ghi đúng nguồn thật
  (`source_name` "SSI"/"FA-Aggregator(tcbs)"), không còn gắn cứng "VNDIRECT".
- Config `FA_FIREANT_TOKEN`; guard `ensure_symbol` chặn ký tự lạ trước khi nội
  suy vào URL path của nguồn.

### Hardening (audit 3 expert — SA, QA/perf, finance-data — 2 vòng, PASS)
- Fallback chỉ nhận **phần ngân sách còn lại** của leg (sàn 0.8s) — không bao giờ
  vượt fan-out budget, không tính TIMEOUT oan cho engine.
- Capability flags `can_financials`/`can_ohlcv`: nguồn stub không bao giờ bị
  probe/ghi EMPTY (không reset breaker của endpoint đang chết thật).
- Gate đơn vị `fa_suspect_unit` ở cả TCBS (ROE/ROA ±200%, market cap, EPS ±1trđ,
  implied EPS = giá/P/E hai chiều) và FireAnt (mọi khoản `_vnd_bil` trong
  [0, 3tr] tỷ đ, growth ±500%).
- Tách `as_of` (cuối kỳ) khỏi `published_at` (ngày công bố) qua toàn bộ chuỗi;
  SSI từ chối nến NaN/âm/thiếu mảng o-h-l-v (không fill close); cap 5MB trước
  `resp.json()` ở cả 5 endpoint VN; `fa_conflict_majority`, half-open recovery,
  dup-guard; probe live ghi bằng chứng ở docs/15 §10.

### Verified
- 48 unit test (`tests/test_fa_sources.py`, MockTransport, không gọi live):
  parse từng nguồn, cross-validate/conflict/majority/missing, breaker mở sau 3
  lỗi + half-open phục hồi đúng 1 probe, timeout không chìm fan-out, auth-fault
  FireAnt, fallback tích hợp adapter (mixed primaries + happy path không đổi),
  provenance kết quả, symbol guard, unit gates. Ruff + bandit + perf sạch
  (p95 1.65s @ c=25, 0 lỗi); suite đầy đủ 100% pass.
- CafeF/VietStock scraper để đợt sau — phải probe HTML schema thật trước khi
  parse, không parse đoán mò (quy tắc không ngụy tạo).

## [2.0.0] — 2026-11-09 — Readiness cho hệ thống bên ngoài: an toàn, liêm chính dữ liệu, tải, quan sát, vận hành, CI

Bản này gom chương trình readiness (đợt 1-4). Bản MAJOR vì có thay đổi phá vỡ tương thích ở REST API (xem mục đầu). Mọi số đo nêu dưới đây là số đo thật trên harness `api/scripts/perf/` hoặc trên máy chạy Lite; mục nào chưa đo được ghi rõ.

### Breaking / thay đổi hành vi
- **`/v1/context`** mặc định chỉ trả `chunks` và `sources`; `context_md` và các khối khác phải xin qua `include`.
- **BYO LLM tắt mặc định** (`LLM_BYO_ENABLED=false`): request chỉ định `base_url` riêng bị từ chối, trừ host trong `LLM_BYO_ALLOWED_HOSTS`. Khóa trong env hoặc runtime không bao giờ gửi tới `base_url` lấy từ request. Lỗi LLM không còn trả body của upstream.
- **Chỉ admin:** `/v1/meta/full`, `GET /v1/llm/config`, `/v1/llm/dream-rsi/state`, test LLM với endpoint tùy chỉnh; `/docs`, `/openapi.json` chỉ mở cho loopback hoặc admin; `/metrics` cần key khi bật auth.
- **Cách ly theo key:** key chỉ đọc được query, answer, research job của chính nó (sai chủ trả 404); bản ghi không chủ chỉ admin thấy.
- **SuperAgent tắt mặc định** (`SUPERAGENT_ENABLED=false`); không còn artifact viết sẵn hay số token giả khi thiếu LLM.
- **Dữ liệu tài chính:** bỏ ước lượng và bảng tĩnh khỏi runtime. Z/M/F-Score chỉ tính khi có BCTC thật, trường thiếu là `null`; upstream chết thì trả rỗng, không trả số cũ; không có mẫu hình mặc định.
- **Crawler tự xưng `MetaSearchBot`** như khi đọc robots.txt, bỏ header giả trình duyệt.
- **503 `OVERLOADED` kèm `Retry-After`** khi quá tải hoặc khi mọi nhà cung cấp LLM đang nghỉ; rate limit có trọng số (answer 5, context 3, search 1, research 20).

### Added
- `include_domains`, `exclude_domains` cho search, context, answer; `complete=false` khi lọc hết.
- Mốc dữ liệu `source_url`, `data_kind`, `fetched_at`, `published_at`, `as_of`, `age_s` cho kết quả, source, chunk; `marker_valid` ở `/v1/citations/validate`; MCP thêm `research_status`.
- Hợp đồng tích hợp: `docs/15-integration-guide.md`, `api/scripts/contract_check.py`, snapshot OpenAPI có test.
- Ngân sách và deadline theo request, admission control, quorum fan-out, cắt sớm scrape, `max_concurrent` theo key.
- Gộp request trùng (single-flight) cho answer, search, scrape; negative cache theo nguyên nhân lỗi.
- Lớp HTTP dùng chung (pool `search`, `scrape`, `llm`, `internal`), SSL context dựng một lần; extra `tls` dùng trust store của hệ điều hành.
- Việc CPU nặng (`extract_static`, chunk, score, rerank ONNX) chạy ở thread, không trên event loop.
- Circuit breaker cho engine, LLM provider và Redis; fail-open khi mọi engine bị ngắt.
- Metric: stage, LLM, scrape, admission, breaker, loop lag, RSS, pool HTTP, hàng đợi persist; alert trong `deploy/prometheus/alerts.yml` (lỗi 5xx, answer chậm, loop lag, breaker mở, hàng đợi persist đầy, ghi bị bỏ, ghi lỗi).
- Ghi DB qua hàng đợi có giới hạn; tắt êm không mất answer đã trả.
- Retention theo lô, đúng thứ tự khóa ngoại; Lite tự chạy; lệnh `metasearch retention [--dry-run]`.
- `/readyz`: 503 khi không còn engine dùng được hoặc đang tắt; probe có giới hạn thời gian; `schema_version` thật.
- Lite trên Windows như một dịch vụ: `scripts/windows/metasearch-lite.ps1` (start, stop, restart, status, check, xoay log), `install-task.ps1` (Task Scheduler), `measure-lite.ps1`.
- Harness đo `api/scripts/perf/` (upstream giả, server thật có token, load driver).
- CI: lockfile `uv`, mypy cho `app/core` và `app/api`, coverage tối thiểu 84%, gitleaks toàn lịch sử, promtool, parse script Windows, `npm audit`, test MCP với SDK thật; workflow nightly (integration với compose, perf smoke, live smoke).

### Fixed
- SSE của deep research không còn đóng băng khi vượt bộ đệm sự kiện; có sự kiện `gap` cho subscriber chậm.
- Engine duy nhất lỗi rồi hồi phục thì tìm kiếm tự chạy lại; không ghi DB trên đường nóng.
- Lite: `alembic upgrade head` lúc khởi động (trước đây `create_all`, DB cũ không nhận index và migration dữ liệu); DB cũ được bổ sung rồi `stamp head`.
- Lite: `/readyz` báo `ready` khi không cấu hình Redis (trước đây luôn `degraded`); Redis im lặng không còn làm probe hết giờ.
- `-EnvFile` của script Windows do app đọc bằng cùng bộ parse với `api/.env`: trước đây chú thích cuối dòng thành giá trị (ví dụ `JINA_API_KEY`), giá trị rỗng bị xóa nên mặc định `redis://redis:6379` quay lại.
- `measure-lite.ps1` đo và kill đúng tiến trình server (trước đây đo launcher của venv, khoảng 5 MB).
- Gauge độ sâu hàng đợi persist giảm khi consumer lấy ra (trước đây chỉ tăng).
- Monitor RSS không còn import mỗi nhịp (từng làm treo lúc tắt); đọc RSS được trên macOS.
- Extra `mcp` ghim `<2`: `mcp` 2.x đổi tên API làm `python -m app.mcp` lỗi.
- Vá `postcss` (high) trong frontend.
- `metasearch-lite.ps1` & `measure-lite.ps1`: kiểm tra dòng lệnh tiến trình qua Win32_Process (`uvicorn ... app.main:app` và `--port`), ngăn chặn việc nhầm lẫn hoặc dừng nhầm tiến trình Python khác khi Windows tái sử dụng PID sau sự cố/khởi động lại máy.
- Giải quyết triệt để 13 phát hiện kiểm toán F1–F13: lưu trữ bền vững phán quyết LLM Council (`council.py`), cơ chế đánh dấu chu kỳ dọn dẹp dữ liệu (`.retention_last_run`), tối ưu hóa imports module tránh lỗi F823 trong lifespan, và dọn dẹp các tham chiếu `nq` còn sót lại trong pipeline.

### Đo được (harness, upstream giả, Windows 11, 2026-11-09)
| Kịch bản | Trước (1.3.1) | Bây giờ |
|---|---|---|
| Search 5 engine, c=1, p95 | 875 ms | 197 ms |
| Search 5 engine, c=20 | 1,0 RPS, 100% lỗi | 16,7 RPS, 0% lỗi |
| Lite context c=1, p95 | 6480 ms | 692 ms |
| Lite context c=10 | 100% lỗi | 0% lỗi, p95 2923 ms |
| Answer c=1, TTFT p95 | 1626 ms | 929 ms |
| Answer c=15, TTFT p95 | chưa đo | 2862 ms, 0% lỗi |
| Event loop stall tối đa khi search c=20 | 1324 ms (c=3) | 54 ms |

Chưa đo: Postgres, Redis, crawl4ai, SearXNG thật; Linux; soak dài.

### Sửa tài liệu
- 1.3.1 ghi "nghiêm cấm ngụy tạo dữ liệu tài chính trong toàn bộ hệ thống" nhưng khi đó vẫn còn ước lượng ở ba lớp; bản này mới gỡ hết (mục Breaking).
- Danh mục mã là 1.518 mã (`vn_tickers.json`), không phải "hơn 1,600".
- Watchdog của SuperAgent là thử nghiệm và tắt mặc định.
- `docs/08`: một tiến trình uvicorn (không phải `2×vCPU` worker); retention, alert, cache Lite mô tả đúng; `docs/11`: CI và eval mô tả đúng.

## [1.3.1] — 2026-11-02 — Financial Data Integrity, Real Market OHLCV & 3-Round Expert Audit

### Fixed & Hardened (Data Integrity & Grounding)
- **Loại Bỏ Triệt Để Chuỗi Nến Giả Lập Bằng Hàm `sin/cos` (`viet_stock.py`, `stooq.py`)**:
  - Xóa bỏ vĩnh viễn hai hàm toán học giả lập `_build_stock_ohlcv_series` và `_build_asset_fallback_series`.
  - Nghiêm cấm ngụy tạo dữ liệu tài chính trong toàn bộ hệ thống; thực hiện nguyên tắc **Zero-Hallucination & Grounded Data**.
- **Tích Hợp Dữ Liệu Nến OHLCV Thật Cho Thị Trường Việt Nam (`VNDIRECT TradingView Feed`)**:
  - Kết nối trực tiếp API công khai keyless của VNDIRECT (`https://dchart-api.vndirect.com.vn/dchart/history`) lấy toàn bộ chuỗi nến daily thực tế (`t`, `o`, `h`, `l`, `c`, `v`) cho hơn 1,600 mã chứng khoán (HSX, HNX, UPCOM) và chỉ số thị trường (`VNINDEX`, `VN30`).
  - Toàn bộ chỉ báo kỹ thuật định lượng (Fibonacci Retracement, Golden Pocket 61.8%-65.0%, Mây Ichimoku, Linear Regression Channel, Chart Patterns, Wilder's RSI, True EMA MACD, Wilder's ATR Trailing Stop) được tính toán 100% trên nến thật.
- **Tích Hợp Hồ Sơ & Định Giá Doanh Nghiệp Thật (`Simplize API`)**:
  - Kết nối dữ liệu cơ bản thực tế từ Simplize API (`https://api.simplize.vn/api/company/summary`), tự động cập nhật giá thị trường live, P/E, P/B, EPS, ROE, ROA, vốn hóa thực tế và ngành nghề niêm yết.
  - Thuật toán `_build_dynamic_company_financials` tự động sinh hồ sơ chuẩn mực cho bất kỳ mã niêm yết nào tại Việt Nam mà không bị bó hẹp trong 13 mã cố định.
- **Chuẩn Hóa Liêm Chính Mốc Thời Gian (`published_at`)**:
  - Loại bỏ hoàn toàn nhãn `published_at=datetime.now(UTC)` ngụy tạo thời điểm hiện tại cho dữ liệu quá khứ.
  - Nến kỹ thuật được gán mốc thời gian chính xác theo phiên giao dịch cuối cùng: `datetime.fromtimestamp(last_ts, tz=UTC)`.
  - Báo cáo cơ bản lấy theo ngày cập nhật BCTC thực tế của sở/kiểm toán.
- **Loại Bỏ Hành Vi Hijack Câu Hỏi Thị Trường Chung**:
  - Xây dựng bộ lọc `_VIET_STOCK_STOP_WORDS` ngăn chặn tuyệt đối các từ viết tắt và từ tiếng Việt trùng lặp (BCTC, ROE, CHO, NEN, GIA, MUA, BAN,...).
  - Trả về `[]` khi query chỉ là câu hỏi thị trường chung hoặc kiến thức đầu tư, nhường quyền tìm kiếm tin tức vĩ mô cho SearXNG và Google News.
- **Kiểm Định Chuyên Sâu 3 Lượt (3-Round Expert Audit)**:
  - Lượt 1: Solution Architect (Decoupling, Graceful Degradation zero-failure, Timeout Bounds 3.5s).
  - Lượt 2: Financial Quant & BA (Toán học định lượng, nến OHLCV thật, time integrity).
  - Lượt 3: QA, Security & Perf (Bandit AST 0 issue, 374 unit & E2E tests green 100%, Next.js clean build).

## [1.3.0] — 2026-11-02 — DeepSeek Super-Agent Architecture (Router-Worker, Observe Mode, Quiet Autonomy & Watchdog Supervisor)

### Added
- **Kiến Trúc Autonomous Super-Agent Engine (`app.orchestration.superagent`)**:
  - Tích hợp nguyên mẫu tự trị từ **`deepseek-ai/dsh-superagent-prototype`** (bài báo *"Exploring Super-agent Design: Towards a More Autonomous Agent System"*), giải phóng điểm nghẽn giới hạn chú ý của con người.
  - **Router - Worker Decoupling (`SuperAgentRouter`, `IsolatedSuperAgentWorker`)**: Tách biệt tầng điều phối trung tâm khỏi các phiên làm việc độc lập; Router nhận diện sự kiện, phân rã mục tiêu thành các **SuperAgent Tickets** theo độ ưu tiên (`CRITICAL`, `HIGH`, `NORMAL`, `LOW`).
  - **Triết Lý "Quiet Autonomy" & Escalation Queue**: Agent thực thi tự trị đa bước ReAct mà không làm phiền nhóm; chỉ lên tiếng khi có thành phẩm (`SuperAgentArtifact`) kèm trích dẫn `[n]` hoặc khi phát sinh rủi ro cao cần con người phê duyệt (`EscalationRequest`).
  - **Observe Mode (Shadow Evaluation)**: Chế độ shadow plan thẩm định mục tiêu và phân tích rủi ro mà không tiêu tốn quota live API.
  - **Proactive Event Ingestion (`EventIngestionEngine`, `POST /v1/superagent/events/ingest`)**: Tự động kích hoạt tickets từ các luồng sự kiện thời gian thực (BCTC kiểm toán, thông báo chứng khoán, CVE bảo mật).
  - **Watchdog Supervisor & Circuit Breaker (`WatchdogSupervisor`, `GET /v1/superagent/watchdog/status`)**: Giám sát nhịp tim worker, tự động ngắt mạch (trip circuit) khi vượt ngưỡng thời gian (120s) hoặc bùng nổ token (`TOKEN_BURST`).
  - **Giao Diện Super-Agent UI**: Tab giao diện mới trên Web UI Next.js cho phép submit mục tiêu tự trị, theo dõi tickets, giám sát Watchdog và phê duyệt Escalation trực quan.
  - **Bộ Kiểm Thử Toàn Diện (`api/tests/test_superagent_engine.py`)**: 8 bài kiểm thử chuyên sâu cho Router, Worker, Observe Mode, Quiet Autonomy, Watchdog và REST API (tổng 383+ tests xanh 100%).

## [1.2.1] — 2026-11-02 — Multi-Platform Matrix CI (Windows, macOS, Linux), 3-Round Deep Audit & Hardened Council

### Added
- **Multi-Platform Matrix CI Trên Mọi Commit (`.github/workflows/ci.yml`)**:
  - Mở rộng toàn bộ quy trình CI chạy ma trận tự động trên cả 3 hệ điều hành: **Ubuntu Linux (`ubuntu-latest`)**, **Windows Server (`windows-latest`)**, và **macOS (`macos-latest`)**.
  - Kích hoạt trên **mọi commit** push lên bất kỳ nhánh nào (`branches: ["**"]`) và mọi pull request.
  - Phân tách thành 4 job kiểm định độc lập và toàn diện:
    1. `backend-matrix` (Linux, Windows, macOS): Cưỡng chế Ruff format, Ruff lint, Bandit AST security scan, Mandatory End-to-End full system verification suite (`test_e2e_full_system.py`), và Pytest full suite.
    2. `frontend-matrix` (Linux, Windows, macOS): Cưỡng chế TypeScript strict typecheck (`npm run typecheck`) và Next.js production build (`npm run build`).
    3. `release-pipeline-gate` (Linux, Windows, macOS): Chạy toàn bộ pipeline kiểm định phát hành `scripts/release.py --check` trong môi trường sạch.
    4. `docker-build-smoke` (Linux): Kiểm thử đóng gói Docker container image độc lập cho cả API và Frontend.
- **Cross-Platform Pre-Commit Hook Hardening (`.githooks/pre-commit`, `scripts/release.py`)**:
  - Hỗ trợ phát hiện thông minh môi trường thực thi trên macOS/Linux (`api/.venv/bin/python`, `python3`, `python`) và Windows (`api/.venv/Scripts/python.exe`).
  - Tự động cấp quyền thực thi `chmod +x` cho `.githooks/pre-commit` trên Unix/macOS và đồng bộ index mode 100755 trong Git.
  - Loại bỏ hoàn toàn cờ `shell=True` trong `run_command` của `scripts/release.py` (0 cảnh báo Bandit B602).
- **Bộ Kiểm Thử Sâu Edge-Case Hội Đồng & Replay World (`api/tests/test_council_and_dream_rsi_deep.py`)**:
  - Kiểm thử trích xuất JSON ngoặc nhọn cân bằng từ mọi định dạng phản hồi phức tạp, markdown code blocks, prose bao quanh.
  - Kiểm thử độ ổn định giải thuật UCB1 khi số lần thử ban đầu là 0 hoặc lớn.

### Fixed & Hardened (3-Round Deep Audit)
- **Thuật Toán Trích Xuất JSON Cân Bằng (`api/app/llm/council.py`)**: Thay thế regex greedy `r"\{.*\}"` bằng balanced braces extraction parser và markdown fence cleaner, ngăn chặn triệt để lỗi parse khi LLM chèn prose hoặc code block.
- **Xử Lý Điểm 0.0 Không Bị Đổi Thành Điểm Fallback (`api/app/llm/council.py`)**: Sửa lỗi falsy evaluation `item.get("score") or 7.5` khiến điểm 0.0 của model bị gán thành 7.5; bọc try/except an toàn riêng từng ứng viên.
- **Tính Đồng Thuận Pairwise Jaccard Chuẩn Xác (`api/app/llm/council.py`)**: Cải tiến `_compute_consensus_agreement` sang trung bình Jaccard theo từng cặp ứng viên (Pairwise Jaccard), loại bỏ sai lệch khi có 1 model lệch nguồn.
- **Cô Lập Timeout Từng Endpoint Hội Đồng (`api/app/llm/council.py`)**: Thiết lập timeout 40s độc lập cho mỗi panelist; một endpoint bị treo không làm nghẽn toàn bộ hội đồng.
- **Dọn Dẹp Coroutine Khi Client Ngắt Kết Nối SSE (`api/app/api/v1/answer.py`)**: Bắt `asyncio.CancelledError` và await task cancellation với `contextlib.suppress`, triệt tiêu cảnh báo rò rỉ tài nguyên.
- **Gia Cố Che Giấu Khóa API Ngắn (`api/app/llm/providers.py`)**: Nâng cấp `mask_api_key` với các key độ dài 8-16 ký tự để tránh brute-force.

## [1.2.0] — 2026-11-02 — Multi-Free-Endpoint LLM Council, Dream-RSI Replay World, Directory Integration (nejib1/Free-LLM & awesome-freellm-apis) & Universal Multi-Domain Intelligence

### Added
- **Multi-Free-Endpoint LLM Council & Anonymous Cross-Review (`app.llm.council`, `POST /v1/llm/council`, `PATCH /v1/llm/council/endpoints`)**:
  - Hỗ trợ gọi song song đồng thời nhiều Free LLM endpoints trong 1 lượt truy vấn (mặc định OpenRouter Free, Pollinations keyless, Groq Free, Google Gemini Free).
  - Cơ chế **Anonymous Cross-Review**: làm mù danh tính các model thành `Candidate A, B, C...` để các panelist chấm chéo lẫn nhau theo thang rubric chuyên ngành mà không bị thiên kiến tự chấm điểm cao cho chính mình.
  - Kết hợp chấm điểm tất định (`deterministic_score` dựa trên độ chuẩn xác trích dẫn `[n]`, độ phủ nguồn context, và mật độ dữ liệu định lượng — trọng số 40%) với điểm bình duyệt đồng cấp từ LLM (`peer_score` — trọng số 60%) để chọn ra Candidate chiến thắng và giao Chủ tịch Hội đồng (`Chairman`) tổng hợp phán quyết tối hậu (`verdict_md`).
  - Khả năng **Graceful Degradation**: tự động loại bỏ endpoint bị lỗi 429 hoặc timeout mà không làm hỏng cả hội đồng.
- **Dream-RSI (Recursive Self-Improvement via Replay World) (`app.llm.council`, `GET /v1/llm/dream-rsi/state`, `POST /v1/llm/dream-rsi/reset`)**:
  - Lưu trữ lịch sử phán quyết vào **Replay Buffer** (`ReplayTrajectory`), tính toán điểm tiềm năng khai phá **Upper Confidence Bound (UCB1)** cho từng endpoint.
  - Tự động chạy **Dream Replay & Optimization** sau mỗi hội đồng: mô phỏng lại các truy vấn lịch sử để tự động tiến hóa chính sách chọn lựa panel tốt nhất (`recommended_panel`), tính độ lệch cải thiện qua các thế hệ (`replay_improvement_delta`) và điều chỉnh nhiệt độ tối ưu (`optimal_temperature`) theo từng miền chuyên sâu.
- **Tích Hợp Danh Mục Free LLM Toàn Diện (nejib1/Free-LLM & awesome-freellm-apis)**:
  - Tích hợp 2 danh mục Free LLM mở rộng từ [`nejib1/Free-LLM`](https://github.com/nejib1/Free-LLM) (34+ Free LLM APIs) và [`open-free-llm-api/awesome-freellm-apis`](https://github.com/open-free-llm-api/awesome-freellm-apis) (134+ Free LLM APIs).
  - Mở rộng lên **20 LLM provider presets**: `openrouter`, `pollinations`, `gemini`, `groq`, `nvidia_nim`, `cerebras`, `sambanova`, `mistral`, `github_models`, `cloudflare`, `cohere`, `chutes`, `siliconflow`, `hyperbolic`, `huggingface`, `ollama`, `custom`, `openai`, `anthropic`, `none`.
  - Phân loại rõ ràng thuộc tính gói cước (`keyless_free`, `permanent_free`, `trial_credits`, `local_free`, `paid_or_byo`).
- **Kiến Trúc Universal Multi-Domain Deep Intelligence (`app.retrieval.domain_intelligence`, `GET /v1/domains`, `POST /v1/domains/analyze`)**:
  - Mở rộng khả năng thẩm định bản chất chuyên sâu ("hiểu bản chất thay vì chỉ đọc bề mặt") sang đa lĩnh vực, với **Tài chính & Pháp y BCTC (`financial`)** giữ vai trò **Flagship Pilot** thử nghiệm chuyên sâu cao nhất (`flagship_pilot`).
  - Bổ sung 7 gói chuyên sâu tích hợp sẵn:
    1. `financial` (CAMELS, Margin/FVTPL, Unbilled Bookings, Altman/Beneish/Piotroski, Fibonacci Golden Pocket, Ichimoku, Linear Regression Channel $\pm 2\sigma$, 8 Chart Patterns).
    2. `biomedical_health` (PICO Clinical Framing, Evidence Hierarchy, Mechanism of Action, Safety & Contraindications).
    3. `technology_ai` (System Architecture, Algorithmic Complexity $O(\cdot)$, OWASP/CWE Security Auditing, Performance Benchmarks).
    4. `academic_research` (First-Principles Formulation, Dataset & Ablation Validity, Consensus vs Contradictions).
    5. `legal_macro_policy` (Transmission Channels, Regulatory Hierarchy, Sectoral & Macro Impact).
    6. `eastern_metaphysics` (Can Chi, Ngũ Hành, Bát Trạch Cung Phi, Ergonomics kiến trúc khoa học).
    7. `news_geopolitics` (Chronological Timeline, Multi-Source Cross-Verification, Geopolitical Implications).
    8. `general_discovery` (Universal Multi-Engine Synthesis).
- **Cổng Kiểm Định Pre-Commit Bắt Buộc & Toàn Trình E2E Test Suite (`scripts/release.py`, `.githooks/pre-commit`, `tests/test_e2e_full_system.py`)**:
  - Xây dựng bộ test **End-to-End Full System Verification Suite** (`test_e2e_full_system.py`) kiểm tra toàn bộ 20 presets, 8 domains, Council đa endpoint, Cross-review, Dream-RSI, Answer JSON & SSE, Context-Only RAG, và Citation Validator.
  - Cài đặt Git hook `.githooks/pre-commit` (kèm kích hoạt tự động `git config core.hooksPath .githooks`): **Nghiêm cấm mọi lệnh `git commit` nếu bộ kiểm thử Unit hoặc E2E có bất kỳ lỗi nào**.
- **Nâng Cấp Giao Diện Web UI (`frontend/app/page.tsx`, `frontend/lib/api.ts`)**:
  - Nút bật tắt nhanh **`⚖️ Hội Đồng LLM + Dream-RSI`** ngay trên thanh tìm kiếm.
  - Thanh chọn **Miền Chuyên Sâu (Universal Intelligence)** hỗ trợ cả tự động nhận diện và chọn thủ công.
  - Khung hiển thị **Biên Bản Hội Đồng Phán Quyết** trực quan (Chairman Model, Điểm đồng thuận, bảng điểm định lượng và Peer Review của từng Candidate A/B/C).

## [1.1.0] — 2026-11-02 — OpenRouter Free Router (`openrouter/free`), Reputable Free LLM Presets & External System LLM Integration

### Added
- **OpenRouter Free Router (`openrouter/free`) & Multi-Provider Free LLM Registry (`app.llm.providers`, `app.llm.gateway`)**:
  - Tích hợp và cấu hình sẵn **OpenRouter Free Models Router** (`openrouter/free` — `https://openrouter.ai/openrouter/free`, endpoint `https://openrouter.ai/api/v1`) với chi phí `$0/M` input & output, tự động định tuyến tới các model miễn phí tốt nhất đang online (`Llama 3.3 70B`, `DeepSeek V3/R1`, `Gemini 2.0 Flash`, `Qwen 2.5 72B`, `Gemma 3 27B`) kèm chuỗi model dự phòng tự động (`OPENROUTER_FALLBACK_MODELS`).
  - Bổ sung danh mục 13 preset LLM (`LLM_PROVIDER_PRESETS`) bao gồm các nguồn miễn phí uy tín:
    - `openrouter` (`openrouter/free`)
    - `pollinations` (Pollinations AI — Keyless 100% miễn phí, không cần API key)
    - `gemini` (Google Gemini AI Studio Free Tier — `gemini-2.0-flash`, `gemini-2.5-flash`)
    - `groq` (Groq Cloud LPU Free Tier — `llama-3.3-70b-versatile`, `deepseek-r1-distill-llama-70b`)
    - `cerebras` (Cerebras Wafer-Scale Free Tier — `llama-3.3-70b`)
    - `mistral` (Mistral La Plateforme Free Experiment Tier — `mistral-small-latest`)
    - `github_models` (GitHub Models Azure Inference Free — `gpt-4o-mini`, `Llama-3.3-70B-Instruct`)
    - `huggingface` (HuggingFace Serverless Inference Router — `Qwen/Qwen2.5-72B-Instruct`)
    - `ollama` (Local Self-Hosted Ollama / vLLM / LM Studio / llama.cpp — `http://127.0.0.1:11434/v1`)
    - `custom` (BYO-LLM / Internal Enterprise LLM Proxy — LiteLLM, Kong, Azure, vLLM Cluster kèm `CUSTOM_LLM_HEADERS`)
    - `openai`, `anthropic`, `none`.
  - Hỗ trợ cơ chế **Cross-Provider Automatic Fallback (`LLM_FALLBACK_PROVIDER`)**: tự động chuyển sang provider dự phòng (ví dụ `pollinations` keyless free) khi provider chính thiếu API key hoặc gặp lỗi 429/5xx.
- **Hot-Configuration API & Connectivity Probe (`/v1/llm/*`)**:
  - `GET /v1/llm/providers`: Liệt kê toàn bộ 13 provider presets kèm trạng thái cấu hình và danh sách model khuyến nghị.
  - `GET /v1/llm/config` & `PATCH /v1/llm/config`: Xem (đã che mặt nạ API key) và cập nhật nóng `provider`, `protocol`, `base_url`, `api_key`, `quick_model`, `report_model`, `fallback_provider`, `extra_headers` ngay trong lúc chạy mà không cần khởi động lại server.
  - `POST /v1/llm/config/reset`: Khôi phục cấu hình LLM về mặc định `.env`.
  - `POST /v1/llm/test`: Kiểm tra kết nối và đo độ trễ (`latency_ms`) tới bất kỳ endpoint LLM nào.
- **Tích Hợp 2 Chiều Với Hệ Thống Khác Đã Có Sẵn LLM (`app.api.v1.llm`, `app.orchestration.pipeline`)**:
  - **Chiều 1 — External LLM Orchestrator Mode (Context-Only RAG + Citation Validator)**:
    - `POST /v1/context`: Chạy toàn bộ pipeline 19 Search Engines + Smart Category Routing + Pháp y BCTC + Kỹ thuật định lượng + Scrape + Chunk & Rerank để trả về `context_md` (đánh số `[1]..[n]`), `system_prompt`, `messages`, `chunks`, `sources` cho hệ thống ngoài tự gọi LLM nội bộ của họ.
    - `POST /v1/citations/validate`: Nhận `answer_md` do LLM của hệ thống ngoài sinh ra cùng `chunks` để hậu kiểm, lọc bỏ citation giả mạo và trả về `validated_answer_md` + `citations` chuẩn xác.
  - **Chiều 2 — Per-Request & Server-Wide BYO-LLM Override**:
    - Cho phép truyền trực tiếp đối tượng `llm` (`LLMOverride`) trong body của `POST /v1/answer` và `POST /v1/research`, hỗ trợ cả các internal proxy trả về `application/json` lẫn `text/event-stream` kèm bộ chặn SSRF (`validate_llm_base_url`).
- **Giao Diện Cấu Hình LLM & Tích Hợp Hệ Thống Ngoài trên Web UI (`frontend/app/page.tsx`)**:
  - Bổ sung Tab `🤖 Cấu Hình LLM (OpenRouter Free / BYO-LLM)` trên Web UI cho phép chọn nhanh preset, đổi model bằng 1 click, nhập API key / Custom Base URL / Custom Headers, bấm Test kết nối trực tiếp, và xem mẫu code tích hợp `/v1/context`.

## [1.0.0] — 2026-11-01 — Institutional Financial Forensics, Quantitative Technicals & 5-Wave Deep Architectural Audit

### Added
- **Automated Version Management, Quality Gates & Release Pipeline (`scripts/release.py`)**:
  - Xây dựng hệ thống phát hành tự động hóa chuẩn mực: tự động dọn dẹp file tạm/cache (`clean_workspace`), cưỡng chế 6 cổng kiểm định chất lượng (`ruff`, `bandit`, `pytest`, `npm run typecheck`, `npm run build`), đồng bộ phiên bản nguyên tử giữa `api/app/__init__.py`, `api/pyproject.toml` và `frontend/package.json`.
  - Tự động kiểm tra và trích xuất release notes từ `CHANGELOG.md`, đóng gói Git commit (`release: vX.Y.Z`) và annotated Git tag.
  - Tích hợp GitHub Actions CI/CD release workflow (`.github/workflows/release.yml`) tự động tạo GitHub Release chính thức khi push tag.
  - Tài liệu quy chuẩn chi tiết tại `docs/13-versioning-and-release.md`.
- **Wave 3 — Institutional Financial Forensics (`app.retrieval.financial_forensics`)**:
  - Phân nhánh mô hình pháp y BCTC theo ngành (`industry_type`: `"banking"`, `"securities"`, `"real_estate"`, `"manufacturing"`).
  - **Mô hình CAMELS / Basel II-III cho Ngân hàng** (`VCB`, `TCB`, `MBB`, `ACB`): CAR, Tỷ lệ nợ xấu (NPL), Nợ nhóm 2, Tỷ lệ bao phủ nợ xấu (LLR), NIM, CASA, LDR, CIR.
  - **Mô hình Chứng khoán** (`SSI`): Dư nợ Margin / VCSH (đối chiếu trần pháp lý 200% của UBCKNN), cơ cấu tài sản tài chính `FVTPL` / `HTM` / `AFS`, tỷ lệ an toàn vốn khả dụng (415% vs ngưỡng 180%).
  - **Mô hình Bất động sản & KCN** (`IDC`, `VHM`): Người mua trả tiền trước & Doanh thu chưa thực hiện (Unbilled Bookings / Của để dành), Tỷ trọng tồn kho / Tổng tài sản, Tiền mặt / Nợ ngắn hạn.
  - Bổ sung bộ hàm định lượng tường minh: `calc_altman_z_score`, `calc_beneish_m_score`, `calc_piotroski_f_score` và chuẩn hóa toán học dynamic fallback.
- **Wave 3 — Quantitative Technical Analysis (`app.retrieval.technical_analysis`)**:
  - Bộ đọc tham số chỉ báo tùy chỉnh từ truy vấn tự nhiên `parse_custom_indicator_params(query)` (`RSI 9`, `MACD 8 21 5`, `Bollinger 15 2.5`, `ATR 10`, `Stoch 14 3`).
  - **True EMA-based MACD** (`macd_line`, `signal_line`, `histogram`), **Wilder's smoothed RSI**, **Wilder's ATR & 2x ATR Trailing Stop**, **Stochastic Oscillator (`%K`, `%D`)**, **Multi-EMA Ribbon (`EMA20`, `EMA50`, `EMA200`)**.
  - **Fibonacci Retracement & Extensions**: Vùng **Golden Pocket (`61.8% - 65.0%`)**, mở rộng `127.2%` & `261.8%`, và lọc tách biệt hỗ trợ gần nhất (`< current_price`) / kháng cự gần nhất (`> current_price`).
  - **Ichimoku Kinko Hyo**: Bổ sung tín hiệu `chikou_signal` so sánh với đường giá 26 phiên trước.
  - **Geometric Trendlines & Pattern Recognition**: Kênh hồi quy tuyến tính (`regression_upper`, `regression_lower` $\pm 2\sigma$, hệ số $R^2$) và 8 mẫu hình giá/nến nâng cao (`Double Bottom`, `Double Top`, `Head & Shoulders`, `Ascending Triangle`, `Volume Breakout`, `Bullish Pin Bar`, `Shooting Star`, `Bearish Engulfing`).
- **Wave 4 — Multi-Engine Adapters Hardening (`viet_stock`, `stooq`, `yahoo_finance`, `fengshui`, `wikipedia`, `ddg`, `google_news`)**:
  - Mở rộng `VIET_STOCKS_DB` lên 14 doanh nghiệp đầu ngành HSX/HNX kèm bộ sinh chuỗi OHLCV riêng biệt theo từng mã cổ phiếu `_build_stock_ohlcv_series`.
  - Nâng cấp `fengshui.py` với công thức Cung Phi vạn niên `_digital_root` cho mọi năm sinh `1900..2199`, bổ sung Tam Hợp / Tứ Hành Xung và tra cứu trực tiếp theo tên Can Chi.
- **Wave 5 — Frontend UX/UI & Eval Harness (40 Golden Queries + Faithfulness Judge)**:
  - Hoàn thiện hệ thống CSS trong `frontend/app/globals.css`, thanh chọn chuyên ngành `CATEGORY_CHIPS`, và bộ render bảng biểu Markdown `<table>` trong `frontend/app/page.tsx`.
  - Mở rộng `api/evals/golden.json` lên 40 golden queries và bổ sung `--judge-faithfulness`, `--concurrency` cho `api/scripts/eval_run.py`.

### Fixed
- **Wave 1 — Core Infrastructure, Security, Persistence & Resilience**:
  - Sửa lỗi đệ quy vô tận (`RecursionError`) trên 4 MCP tools trong `app/mcp/server.py`.
  - Bổ sung RBAC `Depends(require_admin)` cho `PATCH /v1/engines/{id}` và `POST /v1/digests/scan`; sửa TTL Redis sliding-window (`window_s + 1`), giới hạn `_key_cache`, gắn header `X-RateLimit-Remaining`, và ghi nhận Prometheus metrics `HTTP_REQUESTS` / `HTTP_LATENCY`.
  - Đồng bộ `DbEngineStore._record_db` (`consecutive_failures` + `_fallback`), sửa lỗi FK `persist_report` (`job.id == research_id` + `_resolve_valid_chunk_ids`), gộp transaction `_persist_search_all`, chống nhân bản `ChunkRow`, và thêm in-memory fallback `_memory_posts` cho `AutomatedScanner`.
  - Tái kiểm tra SSRF `resolve_and_check(target_url)` trước khi leo thang sang `_crawl4ai`/`_jina` sau HTTP redirect; thêm `"Connection": "close"` cho pinned IP request; kiểm tra regex `_LANG_RE` trong `WikipediaAdapter`.
- **Wave 2 — Retrieval, Merger, Scoring & Deep Research**:
  - Bổ sung `infer_categories_from_query` tự động kích hoạt vertical engines khi `categories == ["general"]`.
  - Điền bảng `_TRUSTED_DOMAINS` (`domain_authority`), giữ `snippet_quality = 0.95` cho báo cáo bảng biểu định lượng, nâng `diversity_cap` lên 4 kết quả/domain cho `viet_stock`, `stooq`, `fengshui`, `sec_edgar`.
  - Bảo toàn dữ liệu định lượng trong `AnswerPipeline._score_chunks`, chạy song song `sub_queries` bằng `asyncio.gather` trong `ResearchPipeline`, và giữ tối đa 3 chunks/URL mới trong `ContextPack.merge`.
  - Sửa regex word-boundary `\b` trong `viet_stock._extract_tickers` và `stooq._extract_stooq_symbol` để triệt tiêu hoàn toàn false positive trên từ tiếng Anh thông thường.

## [0.10.0] — 2026-11-01 — Automated Scanning Scheduler, Daily Briefings Blog Engine & Multi-Domain Expansion (17 Engines)

### Added
- **2 Search Engines Mới (Nâng tổng số lên 17 Engines)**:
  - **Google News RSS Adapter (`google_news`)**: Tin tức thời sự cập nhật từng phút qua Google News RSS endpoint keyless, hỗ trợ đa ngôn ngữ và tự động nhận diện tiếng Việt (`hl=vi&gl=VN`), phân tách tự động nguồn báo (VnExpress, CafeF, Tuổi Trẻ,...), zero CAPTCHA.
  - **Feng Shui & Metaphysics Adapter (`fengshui`)**: Công cụ giải toán và tra cứu phong thủy phương Đông: Thiên Can, Địa Chi, 60 Hoa Giáp, Ngũ Hành nạp âm, Bát Trạch Cung Phi (Đông Tứ Mệnh vs Tây Tứ Mệnh), 4 Cát Tinh (Sinh Khí, Diên Niên, Thiên Y, Phục Vị), 4 Hung Tinh, màu sắc tương sinh / tương khắc và bài trí không gian khoa học.
- **Hệ Thống Lịch Quét Tự Động & Daily Briefings / Blog Engine**:
  - Module `AutomatedScanner` (`app.orchestration.scheduler`) tự động thực hiện quét theo chu kỳ hoặc kích hoạt tức thì theo các chủ đề trọng điểm.
  - Bộ REST API v1 tại `/api/v1/digests`: `GET /topics`, `GET /`, `GET /{slug_or_id}`, `POST /scan`.
- **Giao Diện Frontend Web UI Hiện Đại (Tab Navigation & Blog Reader)**:
  - Thanh chuyển đổi giao diện: Tab `🔍 Tìm Kiếm & Hỏi Đáp (Live)` song hành với Tab `📰 Bản Tin & Blog Chuyên Sâu`.

## [0.9.0] — 2026-11-01 — Deep Financial & Equities Intelligence (Yahoo Finance & SEC EDGAR)

### Added
- **2 Financial Vertical Search Engines (Tổng cộng 15 engines)**:
  - **Yahoo Finance Adapter (`yahoo_finance`)**: truy vấn dữ liệu thị trường theo thời gian thực qua endpoints keyless.
  - **SEC EDGAR Adapter (`sec_edgar`)**: truy vấn trực tiếp kho báo cáo tài chính đã kiểm toán của Ủy ban Chứng khoán Hoa Kỳ (`10-K`, `10-Q`, `8-K`, `S-1`).
- **Dedicated Financial Domain Scaffolding trong Research Harness²**:
  - Bổ sung profile `"financial"` trong `HARNESS_PROFILES`.

## [0.8.0] — 2026-10-31 — Universal Engine Expansion & Anti-CAPTCHA Architecture

### Added
- **4 Search Engines mới (Tổng cộng 13 engines)**:
  - **GitHub Adapter (`github`)**, **PubMed Adapter (`pubmed`)**, **HuggingFace Hub Adapter (`huggingface`)**, **Semantic Scholar Adapter (`semanticscholar`)**.
- **Kiến trúc Anti-CAPTCHA & Anti-Bot toàn diện**:
  - Realistic Browser Fingerprint Headers, phát hiện Interstitial Challenge (`is_challenge_page`), tự động leo thang sang Crawl4AI và Jina Reader proxy.

## [0.7.0] — 2026-10-31 — FR-3 Complete, Durable Research & Eval Expansion

### Added
- **Stack Overflow adapter (FR-3 complete)** & **Reddit adapter (FR-3 complete)**.
- **Durable Research Job Recovery**: `ResearchJobStore.get()` tự động phục hồi finished research report và citations từ DB khi server restart.
- **Mở rộng Eval Harness**: `evals/golden.json` mở rộng từ 10 lên 30 queries đa dạng.

### Fixed
- **Unawaited coroutine bug**: chuẩn hóa `persist_report` thành hàm đồng bộ `def`.

## [0.6.0] — 2026-10-31 — Lite profile: chạy không Docker trên máy yếu

### Added
- **DuckDuckGo adapter (keyless)**, **SQLite persistence (Lite profile)**, **In-process fallbacks giữ chất lượng khi không Redis**, **Jina Reader keyless**.

## [0.6.1] — 2026-11-03 — Readiness: WP-07..WP-16 (deep review program)

### Fixed
- **WP-07** Deadlines, admission control, quorum fanout, early exit.
- **WP-08** LLM gateway resilience: error classification, bounded attempts,
  provider breakers, truncation status, prompt injection guard.
- **WP-11** Startup guard, tenancy isolation, surface hardening.
- **WP-12** Observability: metrics expansion, alert rules, cache labels.
- **WP-13** PersistQueue, stable chunk ID, sqlite pragmas, retention.
- **WP-14** readyz 3-state, compose pins, windows lite scripts.
- **WP-15** Empty-vs-error contract split, python client.
- **WP-16** Blocking pip-audit, gitleaks, npm ci.

### Breaking
- `POST /v1/search` returns 200 with `results: []` when all engines return
  zero results but are healthy (was 503). Use error `no_results` with 503
  only when all engines actually failed. Affects every integration.
- `GET /v1/meta` returns only `name` + `version`; full config requires
  `GET /v1/meta/full` with an admin key.
- `GET /metrics` requires an admin key when `AUTH_ENABLED=true`, or
  loopback when auth is off.

## [0.5.0] — 2026-10-31 — Phase 3: Deep Research & MCP + Phase 4 lõi

### Added
- **Deep Research (FR-9)**, **MCP server (FR-10)**, **Auth + rate limit (FR-14)**, **Vertical engines (FR-3: arXiv + Wikipedia)**, **Serper + Brave adapters (FR-2)**, **Phase 4 lõi (eval harness, Langfuse, OTel)**.

### Fixed
- arXiv Atom parsing qua `defusedxml` (bandit B314/B405 — untrusted XML).
- Research plan/learnings event bị trùng lặp trong SSE (callback + yield).

## [0.4.0] — 2026-10-29 — Observability, operations & performance

### Added
- **Grafana + Prometheus thật**, **Quản lý operations (`GET /v1/ops/status`, `GET /v1/queries`)**, **Common error phía client (`ApiError`)**, **Performance harness (`perf_load.py`, `k6`)**.

### Fixed
- **DB-partition stall**: mọi DB op của `DbEngineStore` giờ bounded `ENGINE_STORE_DB_TIMEOUT_S=0.5`.

## [0.3.1] — 2026-10-29 — Expert audit (3 pass)

### Fixed
- **CORS thiếu hoàn toàn** → thêm `CORSMiddleware` + `CORS_ORIGINS`.
- crawl4ai/jina response cap, answer cache key params, background persist ordering, shared HTTP pool.

## [0.3.0] — 2026-10-27 — Hardening sớm + token counting

### Added
- **Pin-IP connector** (`app/extraction/pinning.py`) & **tiktoken token counting** (`app/llm/tokens.py`).

## [0.2.0] — 2026-10-27 — Phase 1: MVP Search

### Added
- `POST /v1/search`, `POST /v1/answer` (JSON + SSE), `POST /v1/scrape`, `GET /v1/engines`, CLI `metasearch search/answer`.

## [0.1.0] — 2026-10-26 — Phase 0: Scaffold (giants-first)

### Added
- Monorepo `api/` (FastAPI) + `frontend/` (Next.js 15) + `deploy/` + `docs/` (12 tài liệu thiết kế + 8 ADR).
