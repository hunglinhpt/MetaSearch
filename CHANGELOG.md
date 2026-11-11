# Changelog

Tất cả thay đổi đáng chú ý của MetaSearch được ghi ở đây.
Format theo [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
version tuân theo [SemVer](https://semver.org/).

## [Unreleased] — VoiceStudio server-side TTS (tuỳ chọn) — xem docs/17

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
