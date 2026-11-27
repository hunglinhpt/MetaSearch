# 36 — Rà soát toàn diện & kế hoạch nâng cấp theo domain (sau v4.6.0)

> Ngày: 2026-11-26 · Bản rà: `main@20f482b` (v4.6.0, đang chạy Lite :8000) · Loại tài liệu: **plan + audit**
> (phiên này không implement; agent khác nhận việc theo §14). Mọi số đo dưới đây là đo thật trong ngày,
> trên instance tách riêng (cổng 8011 / TestClient, DB tạm) — không ghi vào DB của Lite đang phục vụ.

## 0. Tóm tắt điều hành

Hệ thống rộng (94 operation `/v1`, 13 MCP tool, 43 adapter, 1733 test, coverage 88,4 %, ruff/mypy/bandit
sạch) nhưng **chất lượng lõi đo được thấp hơn ngưỡng tự đặt**, và nhiều tính năng "đa nguồn" đang chạy
một nguồn. Ưu tiên của đợt tới là **độ tin cậy và chất lượng đo được**, không phải thêm bề mặt API.

| # | Phát hiện | Bằng chứng (26/11) | Mức |
|---|---|---|---|
| F1 | Chất lượng retrieval dưới ngưỡng | `eval_v2` (29 câu EN/VI): hit@5 **0,379**, nDCG@10 0,177, MRR 0,259 — ngưỡng hit@5 0,60, nDCG 0,50. Golden VI (25 câu): hit@5 **0,28**, MRR 0,112 | P0 |
| F2 | Engine web tổng hợp chính của Lite bị chặn | `ddg` lỗi 4/4 ("bot-blocked HTTP 202"), breaker mở; Lite chỉ còn `bing_web` cho web chung | P0 |
| F3 | 6/26 engine hỏng do hợp đồng upstream đổi | bluesky 403 (search công khai đòi auth), polymarket 422, semanticscholar 429, reddit ConnectError, gdelt timeout 4/4, openalex 3/4 lỗi | P1 |
| F4 | Mọi truy vấn deep đều `complete=false` | 4/4 truy vấn probe; client không phân biệt "engine chết thường trực" với "lỗi tạm" | P1 |
| F5 | Lịch sử health có lưu nhưng không được dùng | DB `engine_health` có 606 cửa sổ từ 12/11, nhưng `/v1/engines/health` chỉ trả cửa sổ hiện tại (`calls=0` sau restart). Tuần 19–25/11 (DB production, đọc chỉ-đọc): **ddg 1/37 thành công**, bluesky 0/12, reddit 0/12, gdelt 0/9, bing_web 28/36 — **không engine nào bị tự tắt, không có cảnh báo** | P0 |
| F6 | FA "đa nguồn" chạy một nguồn | `fa/compare` VCB/FPT/HPG: 6/16 trường, nguồn duy nhất `simplize`, 10 trường thiếu mỗi mã | P1 |
| F7 | Peer discovery sai ngành | `market/peers?symbol=VCB` → `FDC` (chỉ trùng token "Ngoại thương") | P2 |
| F8 | Không có baseline chất lượng được lưu & không có gate chất lượng trong CI | Không có `evals/reports/`; CI/nightly không chạy eval retrieval/answer | P1 |
| F9 | Module quá lớn, khó bảo trì | `events.py` 4570 LOC, `sentiment.py` 3009, `pipeline.py` 2510, `sentiment_quant.py` 2066, `research.py` 1904; `frontend/app/page.tsx` 2172 LOC / 68 `useState` | P2 |
| F10 | Mã chết trong registry | `dogceo`, `opentdb`, `ipapi` dựng adapter (`registry.py:143-145`) nhưng không có EngineConfig — không thuộc phạm vi sản phẩm | P2 |
| F11 | Vùng coverage thấp ở bề mặt có ghi dữ liệu | `cli.py` 51 %, `api/v1/spaces.py` 59 %, `mcp/server.py` 59 %, `api/v1/extdata.py` 64 %, `memory.py` 70 %, `prefs.py` 70 % | P2 |
| F12 | Nợ vận hành từ lâu chưa đóng | integration test Docker, k6 trên compose, backup + runbook, MCP SDK smoke (AGENTS §2 "Còn lại") | P1 |

## 1. Thang "chuẩn cao nhất" dùng trong plan

Mỗi domain được nâng theo 6 tiêu chí đo được; một hạng mục chỉ "xong" khi có số đo trước/sau:

1. **Chất lượng đo được** — golden set + số liệu trong CI (không tụt so với baseline lưu trong repo).
2. **Trung thực dữ liệu** — không ngụy tạo; mọi giá trị có nguồn + thời điểm; thiếu thì `null` (giữ quy ước AGENTS §4).
3. **Độ tin cậy** — SLO theo engine/nguồn, lịch sử qua restart, tự cách ly nguồn hỏng có lý do rõ.
4. **Hợp đồng ổn định** — chỉ thêm field; OpenAPI snapshot; test guardrail W5.
5. **Bảo mật** — SSRF/TLS/auth/tenancy như hiện hành + kiểm tự động.
6. **Bảo trì** — module ≤ ~1000 LOC, coverage bề mặt ghi dữ liệu ≥ 85 %, không mã chết.

---

## 2. D1 — Search & Retrieval

**Hiện trạng.** 35 engine cấu hình (26 bật ở Lite), RRF + signals, SimHash dedup, tier tên miền, freshness
half-life, BGE reranker tuỳ chọn, query expansion, adaptive router, interleaving A/B.
**Đo:** F1, F2, F3, F4; deep search 3,6 s/truy vấn (chạm trần budget).

| ID | Nâng cấp | Việc cụ thể | Nghiệm thu |
|---|---|---|---|
| S1 (P0) | Khôi phục web tổng hợp cho Lite | (a) tách `ddg` thành 2 đường: `html.duckduckgo.com` + `lite.duckduckgo.com`, phát hiện trang anomaly 202 → `Outcome.RATE_LIMITED` (cooldown dài, không đếm là lỗi parse); (b) thêm 1 engine web keyless thứ ba có điều khoản cho phép (ứng viên: Marginalia Search API, Mojeek API free tier — giants-first, đánh giá ToS trước); (c) hồ sơ Lite tuỳ chọn "SearXNG cục bộ" (pip/venv) ghi trong docs/08 | 4 truy vấn probe §0: ≥ 2 engine web tổng hợp `ok` trên mỗi truy vấn; eval_v2 hit@5 ≥ 0,55 (Lite) |
| S2 (P0) | Baseline + gate chất lượng | Lưu report `eval_v2` (EN/VI + VI) vào `evals/reports/` theo tag version; job nightly chạy eval trên stack mock-free (cần mạng) + so delta; PR đụng `retrieval/`/`orchestration/pipeline.py` phải đính kèm delta | Report v4.6.0 có trong repo; nightly fail khi hit@5 tụt > 0,05 |
| S3 (P0) | Danh mục engine có SLO | Dữ liệu đã có trong `engine_health` (`engine_store.py:941` chỉ đọc cửa sổ hiện tại): thêm tổng hợp 24h/7d (`success_rate_24h`, `success_rate_7d`, `empty_rate` từ `empty_count` đã có ở recent window) vào `/v1/engines/health`; tự **cách ly có lý do** (`disabled_reason="auto:upstream_http_403"`) khi tỉ lệ thành công 24h < 20 % với ≥ 10 lượt — hiện auto-disable chỉ dựa `consecutive_failures ≥ 3` (`config.py:400`), breaker mở thì lỗi không còn được đếm, và `_seed_rows` gọi `clear_legacy_auto_disabled` (`engine_store.py:773`) bật lại mọi engine `auto:` ở mỗi lần khởi động, nên ddg 1/37 lọt; trạng thái cách ly mới phải sống qua restart (chỉ xoá khi recovery probe thành công); rule Prometheus `engine_success_rate_24h < 0.5` | `/v1/engines/health` trả số 24h/7d; replay dữ liệu tuần 19–25/11 → ddg/bluesky/reddit/gdelt bị cách ly kèm lý do |
| S4 (P1) | Sửa/loại engine hỏng | bluesky: chuyển sang endpoint có auth tuỳ chọn hoặc tắt mặc định; polymarket: cập nhật theo API Gamma hiện hành (probe thật trước); semanticscholar: key tuỳ chọn + backoff 429; reddit: tắt mặc định ở Lite khi egress chặn; gdelt: giảm timeout + chỉ bật cho `news`/`deep` | Probe 4 truy vấn: không engine nào lỗi 4/4 mà vẫn bật |
| S5 (P1) | `complete` có nghĩa | Tách `complete` (mọi engine **dự kiến** trả lời) khỏi engine đã cách ly; thêm `degraded_engines[]` | Truy vấn với engine cách ly → `complete=true`, `degraded_engines` liệt kê |
| S6 (P1) | Chọn engine theo truy vấn | Deep hiện fan-out mọi engine (gồm stooq/viet_stock/careers trả 0 kết quả cho truy vấn không liên quan). Dùng router đã có để chọn tập engine theo ý định (code/học thuật/tin VN/tài chính) — deep = rộng **trong** ý định | Latency deep p50 ≤ 2,5 s; số engine "ok-0-kết quả" giảm ≥ 50 % mà hit@5 không tụt |
| S7 (P2) | Reranker mặc định có đo | So `heuristic` vs `bge` (int8) trên golden; bật mặc định nếu thắng ≥ 0,05 nDCG và p95 rerank ≤ 300 ms CPU | Report so sánh trong `evals/reports/` |
| S8 (P2) | Dọn mã chết | Xoá `dogceo`, `opentdb`, `ipapi` khỏi registry + test (F10) | `registry.py` không còn 3 adapter; test xanh |

**Tính năng mới (D1).**
- **N-S1 Query understanding VN**: nhận diện thực thể (mã CK, tên công ty, địa danh, mốc thời gian "quý 3/2026") ngay đầu pipeline bằng resolver alias v4.6 → tự thêm category/freshness và `include_domains` theo ý định. Đo bằng golden VI.
- **N-S2 Search operators chuẩn**: `site:`, `-site:`, `"cụm chính xác"`, `after:/before:` parse server-side thành tham số có cấu trúc (giống `when:` của google_news), áp đồng nhất cho mọi adapter.
- **N-S3 Kết quả có "vì sao xếp hạng"**: trả `ranking_explain` (rrf, consensus, tier, freshness, boosts) — debug cho người tích hợp, đã có tín hiệu trong `ScoredResult`.

## 3. D2 — Extraction / Scrape

**Hiện trạng.** trafilatura → crawl4ai → Jina, SSRF + pinning mỗi hop, robots, PDF theo trang, TLS policy v4.6.

| ID | Nâng cấp | Việc | Nghiệm thu |
|---|---|---|---|
| X1 (P1) | Đo tỉ lệ trích xuất | Metric `scrape_outcome{tier,reason}` đã có; thêm tỉ lệ "nội dung hữu ích" (≥ N từ, không phải trang challenge) theo tên miền vào ops | `/v1/ops/status` có top 10 tên miền thất bại |
| X2 (P1) | Bảng quy tắc theo tên miền | Selector/ghi đè trích xuất cho top tên miền VN (cafef, vietstock, vnexpress…) dạng dữ liệu (JSON), không code | ≥ 90 % trang bài của 10 tên miền VN hàng đầu cho markdown sạch (bộ vàng 50 URL đã lưu HTML) |
| X3 (P2) | Trích bảng & số liệu | Giữ bảng HTML thành markdown table (BCTC, bảng giá) thay vì làm phẳng | Bộ vàng 20 trang có bảng: ô số khớp 100 % |

**Tính năng mới.** **N-X1 Scrape có cấu trúc**: `/v1/scrape` nhận `schema` (như `response_format` của search) → trích JSON có kiểm jsonschema, lỗi thì báo, không bịa.

## 4. D3 — Answer / RAG & Citation

**Hiện trạng.** SSE, citation map sớm, claim check lexical, numeric fidelity token-exact (v4.5), follow-ups,
thread carry, compression, council tuỳ chọn. **Đo:** chưa có baseline answer (eval `--answers --judge` chưa
từng lưu report) — đây là khoảng trống lớn nhất của domain.

| ID | Nâng cấp | Việc | Nghiệm thu |
|---|---|---|---|
| A1 (P0) | Baseline answer | Chạy `eval_v2 --answers --judge` với LLM cấu hình chuẩn; lưu report (context_precision, claim_support, numeric_fidelity, citation_precision) | Report trong repo; ngưỡng hiện có (`claim_support ≥ 0,7`) được đo, không chỉ khai báo |
| A2 (P1) | Verify-in-answer (U4 docs/34) | Câu có `unsupported_markers` → nút "kiểm chứng câu này" gọi `/v1/verify` với câu đó; API trả `claim_checks[]` tuỳ chọn | UI + test; độ trễ thêm 0 khi không bật |
| A3 (P1) | Kiểm chứng NLI tuỳ chọn | Thay/ghép lexical entailment bằng NLI nhỏ (ONNX, đa ngữ) khi có model local; fail-open | Trên bộ vàng claim 100 câu: F1 phát hiện câu không được hỗ trợ tăng ≥ 0,1 |
| A4 (P2) | Câu trả lời có cấu trúc theo ý định | So sánh → bảng; tiến trình → timeline; số liệu → bảng có nguồn từng ô | Golden 20 câu so sánh: 100 % có bảng hợp lệ |

**Tính năng mới.** **N-A1 Answer diff (U3)**: hỏi lại câu cũ sau N ngày → trả phần thay đổi + nguồn mới. **N-A2 Hỏi trên tệp người dùng**: tải PDF/DOCX vào Space tạm (đã có PDF theo trang) rồi trả lời kèm citation trang.

## 5. D4 — Deep Research

**Hiện trạng.** IRCoT + coverage gate, STORM outline, Devil's Advocate, conflicts, SSE resume, cancel.

| ID | Nâng cấp | Việc | Nghiệm thu |
|---|---|---|---|
| R1 (P1) | Eval research có baseline | `scripts/eval_research.py` + `evals/research_golden.json` chạy & lưu report (coverage, citation validity, conflict detection) | Report trong repo |
| R2 (P1) | Ngân sách minh bạch | Event `budget` (thời gian/token/fetch đã dùng vs trần) trên SSE; dừng sớm có lý do | Client thấy budget mỗi vòng |
| R3 (P2) | Tách `research.py` (1904 LOC) | planner / executor / synthesizer / verification thành module ≤ 600 LOC, giữ API | Không đổi OpenAPI; test xanh |

**Tính năng mới.** **N-R1 Research template theo domain** (báo cáo doanh nghiệp VN, so sánh công nghệ, rà soát pháp lý) với outline + nguồn ưu tiên khai báo dạng dữ liệu. **N-R2 Lịch research định kỳ** dùng watchlist (poll-on-demand như hiện hành, không chạy nền đốt budget).

## 6. D5 — Trust layer (verify / timeline)

**Hiện trạng (v4.5.0).** Verdict tất định trung thực; giới hạn đã biết: không bác bỏ được mâu thuẫn tri thức, số liệu khác chỉ ra `insufficient_evidence`.

| ID | Nâng cấp | Việc | Nghiệm thu |
|---|---|---|---|
| T1 (P1) | Bộ vàng verify | 60 tuyên bố VN/EN (đúng / sai số liệu / tin giả / mâu thuẫn tri thức) có nhãn; đo accuracy theo verdict | Report; accuracy `supported` ≥ 0,9, tỉ lệ "supported sai" ≤ 0,05 |
| T2 (P1) | Mâu thuẫn số liệu → `refutes` có điều kiện | Khi ≥ 2 nguồn tier ≥ reference cùng nêu số khác (cùng đơn vị, cùng chủ thể/kỳ) → `refutes` với `numeric_conflict` | Ca "GDP 2024 tăng 9,5%" → `refuted` với ≥ 2 nguồn chính thống |

**Tính năng mới.** **N-T1 Claim Watch (U1)**, **N-T2 Blindspot lens (U2)** — giữ thiết kế docs/34 §6.

## 7. D6 — Tài chính & thị trường VN

**Hiện trạng.** FA aggregator, OHLCV VNDIRECT/SSI, sentiment 7 trụ cột, intel (peers/alerts/event-impact),
events + news-wire (v4.6: precision 1,00 trên bộ vàng). **Đo:** F6, F7; OHLCV/alerts/events/crypto/fx sống.

| ID | Nâng cấp | Việc | Nghiệm thu |
|---|---|---|---|
| M-F1 (P1) | FA đa nguồn thật | Probe lại SSI/TCBS route từ egress hiện tại (docs/15 §10 còn mở); thêm nguồn BCTC keyless đã thẩm định; hiển thị `sources_live[]` | `fa/compare` VCB/FPT/HPG: ≥ 12/16 trường, ≥ 2 nguồn cho ≥ 50 % trường (có `validated`) |
| M-F2 (P1) | Peer theo ngành | Bổ sung trường ngành ICB vào `vn_tickers.json` từ nguồn công khai có `as_of`; peers = cùng ngành cấp 3 + quy mô vốn hoá gần | VCB → nhóm ngân hàng quốc doanh (BID/CTG…); test bộ vàng 20 mã |
| M-F3 (P2) | Tách `events.py` (4570 LOC) & `sentiment.py` (3009) | `events/`: sources, parsers, classify, dedup, wire, calendar; `sentiment/`: lexicon, scoring, quant, benchmarks | Mỗi module ≤ 1000 LOC; OpenAPI không đổi |
| M-F4 (P2) | Gắn mã trên trích đoạn có chấm | Mở rộng bộ vàng `vn_headlines_symbols_gold.jsonl` sang trích đoạn (100 dòng) | precision ≥ 0,95 trên trích đoạn |

**Tính năng mới (D6).**
- **N-F1 Disclosure tracker (U5)**: dòng thời gian công bố thông tin (HOSE/HNX/UBCKNN) gắn với biến động giá bằng `event-impact` đã có.
- **N-F2 Lịch sự kiện cá nhân**: watchlist → lịch GDKHQ/ĐHĐCĐ/KQKD sắp tới (từ `market/events`), xuất iCal.
- **N-F3 Screener có nguồn**: lọc mã theo chỉ số FA/kỹ thuật chỉ trên giá trị có `source`/`as_of` (không bịa ô trống).

## 8. D7 — Tri thức riêng (Spaces / KG / Memory / Hybrid / Watchlist)

**Hiện trạng.** CRUD + tenancy W5 Rule 3; KG LightRAG-lite; memory theo key; hybrid BM25 + cosine.
**Đo:** coverage `spaces.py` 59 %, `memory.py` 70 %, `prefs.py` 70 % (F11).

| ID | Nâng cấp | Việc | Nghiệm thu |
|---|---|---|---|
| K1 (P1) | Test tenancy & lỗi cho bề mặt ghi | Đưa 3 module trên lên ≥ 85 % với test chủ sở hữu A/B, DB chết → fallback, giới hạn | coverage ≥ 85 % |
| K2 (P2) | Embedding L2 có hướng dẫn + đo | AGENTS "Cần cải thiện #1": guide TEI/Ollama, đo hit@5 hybrid dense on/off | Report so sánh |
| K3 (P2) | Xuất/nhập Space | `GET /v1/spaces/{slug}/export` (JSON/markdown) — quyền dữ liệu của người dùng | test round-trip |

**Tính năng mới.** **N-K1 Space từ nguồn theo dõi** (RSS/sitemap) tự làm mới khi poll. **N-K2 Hỏi đáp trên KG có đường dẫn** (trả path thực thể làm bằng chứng).

## 9. D8 — Bản tin / Digest / Podcast / TTS

**Hiện trạng.** Scanner extractive có freshness gate, Expert Briefing Engine (grounding verifier + council), podcast VoiceStudio tuỳ chọn.

| ID | Nâng cấp | Việc | Nghiệm thu |
|---|---|---|---|
| B1 (P1) | Đo grounding bản tin | Lưu tỉ lệ câu bị strip bởi verifier + số nguồn/bản tin vào telemetry; ngưỡng cảnh báo | Ops hiển thị theo kênh |
| B2 (P2) | Bản tin dùng gắn mã v4.6 | Cluster theo mã (symbol_evidence mạnh) cho kênh doanh nghiệp | 0 bản tin gắn sai chủ thể trên 20 mẫu chấm tay |

**Tính năng mới.** **N-B1 Bản tin theo watchlist cá nhân** (văn bản, không chỉ podcast). **N-B2 Bản tin song ngữ** (VI gốc + tóm tắt EN có citation giữ nguyên).

## 10. D9 — Bề mặt tích hợp (REST / MCP / Context / LLM gateway)

**Hiện trạng.** 94 operation, OpenAPI snapshot, W5 guardrails, `/v1/context` + `/v1/citations/validate`,
13 MCP tool, gateway nhiều provider + council.

| ID | Nâng cấp | Việc | Nghiệm thu |
|---|---|---|---|
| I1 (P1) | MCP smoke với SDK thật | `pip install '.[mcp]'` trong CI job riêng; gọi `list_tools` + 3 tool qua stdio | CI xanh; `mcp/server.py` coverage ≥ 80 % |
| I2 (P1) | SDK client chính thức | Sinh client Python/TS từ OpenAPI snapshot (`api/clients/` đã có thư mục) + test hợp đồng | Gói client có version theo API |
| I3 (P2) | Webhook đầu ra có ký | Watchlist/Claim Watch/alerts có thể đẩy về URL đăng ký (HMAC, retry bounded, SSRF-checked) — vẫn tắt mặc định | test chữ ký + SSRF |
| I4 (P2) | Versioning field | Ghi `x-since: 4.6.0` trên field mới trong OpenAPI để người tích hợp biết | snapshot có chú thích |

## 11. D10 — Web UI

**Hiện trạng.** 10 tab, theme 3 chế độ, DESIGN.md khoá token (v4.5), 130 vitest. **Đo:** `page.tsx` 2172 LOC / 68 `useState` (F9); không có e2e trình duyệt; chưa kiểm a11y tự động.

| ID | Nâng cấp | Việc | Nghiệm thu |
|---|---|---|---|
| U1 (P1) | Tách `page.tsx` | Mỗi tab một component + hook dữ liệu riêng (Search, Blog, LLM, SuperAgent còn nằm trong page) | `page.tsx` ≤ 400 LOC; vitest xanh |
| U2 (P1) | E2E + a11y | Playwright (pinned) chạy trên `build:export` + axe-core: 5 luồng chính (search, answer SSE, verify, news, compare) | CI job; 0 vi phạm axe mức serious |
| U3 (P2) | Hiển thị bằng chứng v4.6 | Tab Tin tức hiện `symbol_evidence`/`category_reason` khi hover; lọc theo độ tin | test component |

**Tính năng mới.** **N-U1 Command palette** (Ctrl+K: chuyển tab, tìm mã, chạy verify). **N-U2 Chia sẻ kết quả** (permalink chỉ-đọc cho answer/verify/timeline, tôn trọng tenancy).

## 12. D11 — Nền tảng (bảo mật, quan sát, hiệu năng, vận hành, CI)

| ID | Nâng cấp | Việc | Nghiệm thu |
|---|---|---|---|
| P1 (P1) | Đóng nợ vận hành | Integration test trên compose (`METASEARCH_INTEGRATION=1`), k6 trên compose, backup DB + runbook khôi phục (Lite SQLite + Postgres) | Runbook có lệnh đã chạy thật; báo cáo k6 lưu repo |
| P2 (P1) | SLO & cảnh báo | Định nghĩa SLO: search p95 < 1,5 s (non-deep), answer TTFT p95 < 4 s, success engine web ≥ 95 %; rule Prometheus + dashboard | promtool test xanh |
| P3 (P2) | CSP cho UI tĩnh | Thêm CSP có hash cho script bootstrap theme (hiện chỉ `/v1` có CSP) | header + test |
| P4 (P2) | Ngân sách phụ thuộc | pip-audit/npm audit đã có; thêm Renovate/Dependabot nhóm theo tuần + ghi chính sách pin | PR tự động có test |

## 13. Thứ tự triển khai (wave)

| Wave | Mục tiêu | Hạng mục | Điều kiện đóng |
|---|---|---|---|
| W1 (tuần 1) | **Đo được & chạy lại được** | S3, S2, A1, R1, T1, S8 | 4 report baseline trong `evals/reports/`; health qua restart |
| W2 (tuần 2) | **Khôi phục retrieval** | S1, S4, S5, S6 | eval_v2 hit@5 ≥ 0,55 (Lite); 0 engine lỗi 4/4 đang bật |
| W3 (tuần 3) | **Dữ liệu tài chính thật đa nguồn** | M-F1, M-F2, M-F4 | §7 nghiệm thu |
| W4 (tuần 4) | **Bảo trì & UI** | M-F3, R3, U1, U2, K1, I1 | module ≤ 1000 LOC; e2e + a11y trong CI |
| W5+ | **Tính năng mới** | A2, N-S1, N-F1, N-T1, N-U1, I2… theo thứ tự giá trị | mỗi tính năng có golden/test riêng |

Nguyên tắc: không mở tính năng mới (W5) khi gate chất lượng W1–W2 chưa xanh — tính năng mới đứng trên
retrieval đang dưới ngưỡng sẽ thừa hưởng lỗi đó.

## 14. Cách nhận việc (cho agent khác)

1. Chọn **một** ID; ghi tên mình + ngày vào cột "Người nhận" của bảng theo dõi trong `docs/12-roadmap-plan.md`
   (thêm dòng `- [ ] <ID> — <người nhận> — <ngày>`).
2. Trước khi sửa: chạy baseline liên quan và lưu số "trước" (lệnh dưới); sau khi sửa: số "sau" + delta vào PR.
3. Tuân thủ AGENTS §4 và §6 (3 hard boundary rules, OpenAPI snapshot, không đụng dữ liệu Lite thật).

```bash
cd api
# instance tách riêng, DB tạm (không ghi vào DB Lite đang chạy)
SEARXNG_ENABLED=false DATABASE_URL=sqlite+aiosqlite:///./eval_tmp.db REDIS_URL= LLM_PROVIDER=none \
  SERVE_UI=false SCHEDULER_ENABLED=false .venv/Scripts/python.exe -m uvicorn app.main:app --port 8011 &
.venv/Scripts/python.exe scripts/eval_v2.py --base-url http://127.0.0.1:8011 --report-dir evals/reports
.venv/Scripts/python.exe scripts/eval_v2.py --base-url http://127.0.0.1:8011 \
  --golden evals/golden_v3_vi.json --report-dir evals/reports
```

Lưu ý: `eval_tmp.db` là dữ liệu tạm của eval — xoá sau khi dùng là an toàn (không phải DB tích hợp), khác với
`api/metasearch_lbs.db` / `api/.env.*` thuộc diện cấm xoá ở AGENTS §4.

## 15. Phụ lục — số đo gốc 26/11

- eval_v2 (Lite, 29 câu): hit@5 0,3793 · hit@10 0,5172 · recall@10 0,2414 · MRR 0,2588 · nDCG@10 0,1766.
- golden_v3_vi (25 câu): hit@5 0,28 · hit@10 0,44 · MRR 0,1124 · nDCG@10 0,0963.
- Probe deep 4 truy vấn: lỗi — ddg (bot-blocked 202), bluesky (403), polymarket (422), semanticscholar (429),
  reddit (ConnectError), gdelt (timeout ×4), openalex (3/4); ok-0-kết quả — careers, huggingface,
  stackexchange, stackoverflow, stocktwits, stooq, viet_stock, vn_wire; mỗi truy vấn 3,6 s, `complete=false`.
- FA: `fa/compare` VCB/FPT/HPG 1,0 s — 6/16 trường, nguồn `simplize`; OHLCV FPT 30 ngày: 22 nến VNDIRECT;
  alerts FPT/VCB 1 cảnh báo/mã; events FPT: 6 sự kiện, 5 nguồn, 0 lỗi; crypto/fx: sống.
- Health DB production 19–25/11 (thành công/lượt): ddg 1/37, bing_web 28/36, bluesky 0/12, reddit 0/12,
  gdelt 0/9, semanticscholar 0/3, careers 31/40, google_news 163/165, fin_rss 98/98, stooq 110/110.
- Chất lượng mã: 1733 test pass, coverage 88,37 %; module lớn nhất & coverage thấp nhất như §0.

## 16. Kết quả W1 (2026-11-26, cùng phiên — tự kiểm, không độc lập)

| ID | Trạng thái | Kết quả đo được | Ghi chú |
|---|---|---|---|
| S8 | ✅ | `dogceo`/`opentdb`/`ipapi` gỡ khỏi registry, mã và test | IP geo vẫn có ở `/v1/net/ipgeo` |
| S3 | ✅ | Phát lại dữ liệu production 12–25/11 (chỉ đọc): **ddg, bluesky, reddit (và gdelt ngày 20/11) bị cách ly; 0 engine khoẻ bị gắn cờ nhầm** (sec_edgar 91/100, google_news 163/165 không bị đụng) | `orchestration/engine_quarantine.py`; lý do `quarantine:` sống qua restart (chỉ `auto:` bị xoá khi seed); gỡ khi probe không lỗi hoặc PATCH thủ công (lần đánh giá sau chỉ tính lượt gọi mới); `/v1/engines/health` thêm `calls_24h`, `success_rate_24h`, `calls_7d`, `success_rate_7d`, `quarantined`; gauge `metasearch_engine_success_rate_24h`, `metasearch_engine_quarantined` + 2 alert. Luật ban đầu (≥10 lượt/24h + 6h) **không cách ly gì** trên dữ liệu thật vì Lite chỉ 4–8 lượt/ngày → đổi sang cửa sổ bằng chứng 24h-rồi-7d + độ mới 24h. |
| S2 | ✅ | Baseline lưu tại `api/evals/baselines/` (golden_v2 hit@5 0,379; golden VI 0,28); `app/eval_gate.py` + `scripts/eval_gate.py` (tụt > 0,05 → exit 1; khác golden → exit 2); job nightly `quality-eval` (report-only trên runner hosted vì egress khác production) | Report chạy hằng ngày để local (`api/evals/reports/` gitignored) |
| T1 | ✅ | Bộ vàng 60 tuyên bố (`evals/verify_golden.json`). **Trước sửa (v4.6.0): false_support_rate 0,53**, precision 0,63, recall 0,90. **Sau sửa: false_support_rate 0,00, precision 1,00, không tuyên bố đúng nào bị bác bỏ** (2 lần chạy liên tiếp); recall 0,40 | 7 chốt mới trong `claim_verify.py` (§16.1). Recall giảm là đánh đổi có chủ đích (an toàn trước); phục hồi qua A3 (NLI/LLM judge). |
| R1 | ⚠️ một phần | `scripts/eval_research.py` chạy được, baseline lưu (`eval_research.offline.json`), mọi chỉ số 1,0 | Bộ offline chỉ có **2 ca** dựng sẵn — kiểm bộ chấm, **chưa đo chất lượng research thật**; cần bộ ≥ 20 câu chạy research live (mục mới R1b) |
| A1 | ❌ chặn | Không đo được: 0–1/29 câu trả lời thành công trên provider keyless | Lộ **2 lỗi production** (§16.2) — mục mới A0 (P0) |

### 16.1 Các chốt an toàn mới của `/v1/verify` (mỗi chốt có test trong `tests/test_v47_verify_safety.py`)

1. Chấm theo **từng câu** (bỏ cửa sổ 2 câu — thực thể bị thoả bởi hai câu khác nhau).
2. **Thuật ngữ then chốt bắt buộc** (`key_terms`): mọi con số *kể cả năm* + mọi từ viết hoa (trừ hậu tố pháp nhân Inc/Corp/JSC/CTCP…); thiếu → không bao giờ "supports"; trả `missing_terms` trong bằng chứng.
3. **Thứ tự** (LCS ≥ 0,6) và **từ quan hệ** (≥ 67 % từ không phải thực thể).
4. **Số liệu neo đúng quan hệ**: ≥ 60 % từ quan hệ nằm trong ±60 ký tự quanh con số.
5. Câu **rào đón / tương phản / ý định** (may, than just, có thể, đơn xin, dự kiến, plans to, applied…) và **câu hỏi** không ủng hộ (trừ khi chính tuyên bố cũng như vậy).
6. Tín hiệu **myth/tin đồn/bác bỏ ở câu lân cận** chặn "supports" nhưng **không** được kết luận "refutes".
7. "Refutes" (do tín hiệu hoặc lật phủ định) phải chặt ít nhất như "supports": cùng thực thể, cùng thứ tự, cùng quan hệ.

### 16.2 Phát hiện mới trong W1 → bổ sung plan

| ID mới | Mức | Phát hiện | Bằng chứng | Hướng xử lý |
|---|---|---|---|---|
| **A0** | **P0** | `/v1/answer` hỏng trên cấu hình Lite (pollinations keyless) | Instance tách riêng, 29 câu tuần tự: provider trả **HTTP 413** (cả với `ANSWER_CONTEXT_BUDGET=2000`) → API trả **500 internal** → gateway cooldown → mọi request sau **503 "every LLM provider is cooling down"**; 0/29 thành công | (a) 413 là lỗi theo-request: không đếm vào health provider, ánh xạ `upstream_*` 502, thử lại 1 lần với context ½; (b) đo ngưỡng payload thật của provider và đặt trần theo provider; (c) test hồi quy 413 → không cooldown |
| **A0b** | P1 | Provider keyless chỉ cho **1 request đồng thời/IP**; query expansion cũng gọi LLM ở mỗi search | Log: `429 RATE_LIMITED "The Classic API answers 1 request at a time per IP"` khi 2 request song song | Semaphore theo provider (`max_inflight`), xếp hàng thay vì 429 + cooldown; tự tắt expansion khi provider chỉ có 1 slot |
| **R1b** | P1 | Không có eval research live | §16 R1 | ≥ 20 câu research_golden live + lưu baseline |

Thứ tự W2 cập nhật: **A0 → A0b → S1 → S4 → S5 → S6 → A1 (đo lại)**. A0/A0b chặn mọi số đo câu trả lời và ảnh hưởng người dùng `/v1/answer` thật, nên đi trước khôi phục retrieval.

## 17. Kết quả W2 (2026-11-26 → 27, cùng phiên — tự kiểm, không độc lập)

Mọi số đo trên instance tách riêng (DB tạm trong scratchpad, `LLM_PROVIDER=none` cho retrieval,
`pollinations` cho A1), **không** chạy vào Lite :8000.

| ID | Trạng thái | Kết quả | Ghi chú |
|---|---|---|---|
| A0 | ✅ | **A1: 12/12 câu trả lời thành công** (golden_v2, tuần tự, giãn 50 s), 11/12 có citation, 7,6–12,8 s/câu — trước: 0/29 | Provider tự báo: 413 "16,000 characters max", 402 `FREE_LIMIT` "1 reply every 45 seconds per IP. Retry after 40s", 503 "Retry after 10s". 413 trung tính với breaker; trần prompt theo preset + học từ body 413; cắt nguồn đuôi cho vừa trần; thử lại 1 lần với ½ ngữ cảnh; quota → 503 + `Retry-After` thật; lỗi provider → `upstream_error` 502 (trước 500). Baseline `evals/baselines/eval_answer_keyless.lite.json` |
| A0b | ✅ | Slot theo provider (`max_inflight`); provider 1 slot bỏ expansion, follow-ups, LLM rewrite | Với quota 1 câu/45 s, mỗi request chỉ được 1 lời gọi LLM — trước đó expansion tiêu mất lượt của câu trả lời |
| S1 | ✅ một phần | Engine mới `mwmbl` (probe 4/4, 0,3–1,8 s, có kết quả tiếng Việt). golden_v2 hit@5 **0,379 → 0,483** (ngưỡng 0,55: đạt 0,552 ở 1 lần đo, dao động 0,45–0,55 giữa các lần do engine live) | ddg: html/GET/lite đều 202 anomaly từ egress này → không tách đường; 202 → rate-limited 900 s. Marginalia public: timeout (rate-limit chung); Mojeek: captcha |
| S4 | ✅ | Probe 4 truy vấn: bluesky 403 ×4, polymarket 422 ×4, semanticscholar 429 ×4, reddit ConnectError ×4, gdelt timeout/429 (phản hồi 17,7 s) | polymarket sửa sang `/public-search`; 4 engine còn lại tắt mặc định (semanticscholar bật khi có key). DB cũ giữ cấu hình cũ — cách ly S3 tự xử lý |
| S5 | ✅ | `quarantined` status + `degraded_engines[]`; `complete` không còn bị engine cách ly kéo về false | `skipped_breaker` (tạm thời) vẫn làm `complete=false` |
| S6 | ✅ | Deep chỉ luôn bật `general`+`reference` | Lần đo đầu với `academic` luôn bật: arxiv/doi.org lên top cho "review MacBook Air", "so sánh machine learning và deep learning" |

### 17.1 Phát hiện mới khi đo (đã sửa)

1. **Tin tài chính chôn câu trả lời kỹ thuật**: Lite kéo `bing_news/google_news/fin_rss` vào *mọi* truy vấn (thêm hồi chỉ có DDG). "PostgreSQL logical replication" → top là marketwatch/cnbc. Nay: chỉ `wikipedia` luôn đi kèm; truy vấn tiếng Việt thêm `bing_news/google_news`; engine đi kèm tối đa 3 kết quả (google_news từng chiếm 10/10 chỗ cho "review MacBook").
2. **fin_rss khớp bừa**: trùng 1 từ là giữ; và `tokenize` bỏ dấu làm "nội"="nơi", "ngón"="ngon" → "cách nấu phở bò" trả "chanh ngón tay giá 2 triệu". Nay: ≥ ⅓ số từ (tối thiểu 2) và giữ dấu khi truy vấn có dấu.
3. **[Safety] `bing_web` trả trang khiêu dâm** cho "hướng dẫn dùng FastAPI dependency injection" (mkt vi-VN). Bing RSS với `adlt=strict` trả rỗng → lọc phía mình (`app/retrieval/safety.py`, bật mặc định, mọi engine).

### 17.2 Số đo cuối (1 lần, so baseline v4.6.0)

| bộ | hit@5 | hit@10 | MRR | nDCG@10 |
|---|---|---|---|---|
| golden_v2 (29) | 0,379 → **0,483** | 0,517 → 0,517 | 0,259 → 0,350 | 0,177 → 0,258 |
| golden VI (25) | 0,28 → **0,12** ❌ | 0,44 → 0,24 | 0,112 → 0,081 | 0,096 → 0,063 |

### 17.3 Việc còn mở sau W2

| ID | Mức | Việc |
|---|---|---|
| **E1** | P0 | **Hồi quy golden VI chưa giải thích hết.** Bộ VI chấm theo *tên miền* (vnexpress, cafef…) nên tin không liên quan từ cùng tên miền cũng tính là trúng — đã kiểm chứng một phần (fin_rss cũ). Cần: nhãn mức URL/tiêu đề cho 25 câu, chấm lại cả v4.6.0 lẫn hiện tại, rồi mới tạo baseline mới. **Không** ghi đè baseline cũ để che hồi quy. |
| E2 | P1 | Eval live dao động ±0,05–0,10 hit@5 giữa các lần (engine live, rate limit): chạy 3 lần lấy trung vị, hoặc ghi lại phản hồi engine để chấm lại (record/replay). |
| S1b | P1 | Chất lượng tiếng Việt của `bing_web` kém ("kiến trúc microservices" → trang về con kiến; "Redis pub sub" → flashscore). Tìm nguồn web tiếng Việt keyless thứ hai hoặc hồ sơ "SearXNG cục bộ" (S1c). |
| R1b | P1 | Eval research live ≥ 20 câu (chưa làm). |
| A1b | P2 | Đo A1 đủ 29 câu + chất lượng (claim_support, citation precision) — cần ~30 phút do quota 45 s/câu. |

## 18. Tham chiếu XERJ (github.com/xerj-org/xerj) — đánh giá 27/11

**Bản chất.** Index engine một binary Rust (Apache-2.0; model "Query-1" chỉ có trong binary
release, độc quyền), API tương thích Elasticsearch 8.x, MCP stdio/HTTP, BM25 + HNSW + hybrid RRF
(k=60), agent memory theo namespace, "autoindex" thư mục. ~21,6k sao, bản `v1.0.0-rc.101–104`
(chưa GA). Mọi số benchmark (so ES 8.13.4, Query-1, rerank BEIR) **do dự án tự báo cáo**.

**Kết luận.** Khác loại với MetaSearch (meta-search trên engine ngoài, không có index web
riêng) → **không tích hợp làm dependency**; học khuôn mẫu.

| Mẫu XERJ | Áp dụng | Trạng thái |
|---|---|---|
| `_rerank.applied:false` khi reranker chậm/lỗi; 502/503/403 tách bạch | `rerank: {reranker, applied, reason}` trên `/v1/answer`, `/v1/context` | ✅ đã làm (test `test_reranker_fail_open_is_reported`) |
| Lỗi theo-request khác lỗi theo-node (bulk 429 → giảm concurrency, gửi lại phần bị từ chối; 400 chỉ từ chối dataset đó) | Trùng ý A0/A0b: 413 trung tính, quota → chờ đúng `Retry-After`, slot theo provider | ✅ (A0/A0b) |
| Công bố lỗi tích hợp reranker tự phát hiện (pilot nDCG 0,38 vs BM25 0,775) và "xác suất rerank không hiệu chỉnh, đừng ngưỡng `min_score` thô" | Củng cố S7: chỉ bật reranker mặc định sau khi đo trên golden; không dùng điểm rerank làm ngưỡng tuyệt đối | backlog S7 |
| Query-1 trả `ANSWER` / `CONFLICT` / `UNKNOWN` / `UNSUPPORTED`, từ chối thay vì đoán | **X1**: `/v1/answer` thêm trạng thái từ chối (`insufficient_evidence`, `conflict`) dựa trên `claim_verify` + độ phủ nguồn | backlog (P1) |
| `_passage` trả `start_offset/end_offset/page` (giảm payload 34×) | **X2**: offset ký tự + trang trong citation/context chunk để client tô sáng đúng đoạn | backlog (P2) |
| Memory `dedup_threshold: 0.95` | **X3**: khử trùng gần-giống khi ghi `memory_items` (hiện không có) | backlog (P2) |
| ES-wire API | **X4**: backend tuỳ chọn cho Spaces trỏ tới một node ES/XERJ nội bộ — chỉ khi có nhu cầu index cục bộ lớn | ý tưởng, chưa lên plan |

## 19. Đợt 3 (2026-11-27) — hoàn tất việc dở sau W2

### 19.1 E1/E2 — đo lại hồi quy VI cho công bằng

Bản v4.6.0 gốc (`git archive HEAD`) và bản hiện tại chạy song song trên 2 instance tách riêng,
`CACHE_ENABLED=false`, cùng eval, xen kẽ, trung vị 2 lần (`eval_v2.py --repeat 2`). Metric mới
**strict hit@k**: trúng chỉ khi đúng tên miền *và* tiêu đề/snippet khớp chủ đề truy vấn
(`eval_metrics.topical_match`, giữ dấu khi truy vấn có dấu).

| bộ | bản | hit@5 | strict@5 | hit@10 | strict@10 | MRR |
|---|---|---|---|---|---|---|
| golden_v2 (29) | v4.6.0 | 0,362 | 0,293 | 0,448 | 0,379 | 0,200 |
| golden_v2 (29) | hiện tại | **0,552** | **0,552** | 0,586 | 0,586 | 0,390 |
| VI (25) — trước sửa fin_rss | hiện tại | 0,16 | 0,16 | 0,28 | 0,24 | 0,087 |
| VI (25) | v4.6.0 | 0,34 | 0,26 | 0,48 | 0,36 | 0,121 |
| VI (25) | hiện tại | 0,26 | **0,26** | 0,36 | 0,32 | 0,120 |

Kết luận E1: hồi quy VI ở §17 = **nhiễu metric** (bản cũ: 0,08 điểm hit@5 là trang cùng tên miền
nhưng lạc đề) **+ một lỗi thật**: `fin_rss` (thuộc họ `news`) không còn tham gia câu hỏi tài chính
sau khi bỏ khỏi fallback → mất bài VnExpress/VnEconomy đúng chủ đề cho "giá cổ phiếu VIC",
"VN-Index có bao nhiêu mã". Sửa: `_INTENT_EXTRA_ENGINES` (ý định `financial` kéo `fin_rss`).
Sau sửa: strict@5 hoà v4.6.0, strict@10 −0,04. Baseline mới (`eval_v2_golden_*.lite.json`,
có strict, trung vị 2 lần không cache); baseline v4.6.0 giữ ở `*.v460.json`. Gate mặc định so
thêm `strict_hit_at_5`; nightly chạy `--repeat 2` với `CACHE_ENABLED=false`.

### 19.2 Đã triển khai

| ID | Kết quả |
|---|---|
| X1 | `grounding` trên `/v1/answer` (`grounded`/`partial`/`weak`/`ungrounded` + số câu có/không được hỗ trợ, độ phủ citation, numeric fidelity); UI hiển thị nhãn "bám nguồn". Không chặn câu trả lời — khai báo trung thực để client quyết định. |
| X2 | `start_offset`/`end_offset`/`page` cho mỗi chunk (`chunker.assign_offsets`, neo 8 từ không phân biệt khoảng trắng; không tìm thấy → `null`, không đoán) trên `/v1/context`. |
| X3 | Khử trùng memory: `query`/`observation` trùng chuẩn hoá hoặc Jaccard từ ≥ 0,9 trong 50 bản ghi gần nhất bị bỏ qua; `feedback` (phiếu bầu) không bao giờ bị khử. |
| R1b | Harness `scripts/eval_research_live.py` + `evals/research_live_golden.json` (20 câu, VI/EN, 6 domain; từ khoá chỉ là chủ đề, không mã hoá đáp án). **Chưa chạy đủ**: research cần 10–15 lời gọi LLM, pollinations keyless cho 1 câu/45 s → ~3 giờ cho 20 câu; cần LLM có key. |
| S1b | Không có nguồn web tiếng Việt keyless hợp lệ: Cốc Cốc/Qwant không có API công khai, ToS cấm scrape; DDG bị chặn. Đường sạch: adapter `brave` sẵn có (free tier cần key) hoặc SearXNG cục bộ. Không viết scraper vi phạm ToS. |
| Safety | Dấu hiệu tiêu đề chỉ áp với nguồn không có tier tin cậy (báo chính thống đưa tin *về* chủ đề này); dùng tên miền gốc với link bọc Google News. |

### 19.3 A1b và audit cuối đợt

- **A1b**: golden_v2 29 câu qua `/v1/answer` (pollinations keyless, giãn 48 s): **29/29 trả lời**,
  citation precision 1,00, claim_support 0,47, numeric fidelity 0,68, context precision 0,30.
  claim_support < 0,5 là việc tiếp theo của D3 (A2/A3 — câu trả lời bám chunk chặt hơn), giờ đã đo được.
- **Audit (tự kiểm, đọc lại toàn bộ diff W1–đợt 3; skill review đa agent không chạy được vì hết hạn mức)**:

| # | Mức | Lỗi | Sửa |
|---|---|---|---|
| A1 | P2 perf | `assign_offsets`: neo không thấy → quét đến cuối tài liệu cho mỗi chunk (bậc hai trên tài liệu lớn) | cửa sổ tìm có trần; test 1 MB + 300 neo hỏng < 3 s |
| A2 | P2 | polymarket `_parse_dt` có thể trả datetime không múi giờ → lỗi khi tính freshness | ép UTC; test |
| A3 | P3 | set id tính lại trong comprehension (`_fanout`) | tính một lần |
| A4 | **P1** | mốc gỡ cách ly / operator bật lại chỉ trong RAM → restart làm engine vừa gỡ bị cách ly lại bằng lỗi cũ (flapping) | `disabled_reason="released:<ISO>"` trên engine đang bật, evaluator đọc lại sau restart; test "restart sau khi gỡ" |
| A5 | P2 | council lỗi cả panel vẫn trả `internal` 500 | `LLMUpstreamError` → `upstream_error` 502 |
| A6 | P2 | PATCH `enabled:true` lên engine đang bật (sửa weight) ghi mốc release → xoá bằng chứng cách ly | chỉ ghi mốc khi engine đang tắt; test `test_patch_enabled_on_enabled_engine_keeps_evidence` |
| — | info | `/v1/engines` hiển thị `disabled_reason="released:…"` cho engine đang bật | có chủ đích (mốc kiểm toán), ghi ở đây |

Còn mở: R1b chạy đủ (cần LLM có key) · S1b nguồn web VI (cần key Brave/SearXNG cục bộ) · claim_support câu trả lời 0,47.
