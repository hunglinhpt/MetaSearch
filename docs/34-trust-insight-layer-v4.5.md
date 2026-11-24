# 34 — Trust & Insight Layer (v4.5.0) + đánh giá stitch-skills + roadmap đề xuất

> Ngày: 2026-11-24 · Phiên bản: v4.5.0 · Trạng thái: ✅ triển khai (phần §3–§5), 📋 đề xuất (phần §6).
> Lưu ý quy trình: phiên này vừa lập phương án vừa implement theo yêu cầu trực tiếp của chủ dự án,
> nên các vòng audit ở §7 là **tự kiểm** (không độc lập) — xem [AGENTS.md §6](../AGENTS.md).

## 1. Bối cảnh — hệ thống đang ở đâu

Khảo sát `main@4070bcd` (v4.4.2, working tree sạch): ~62k LOC Python (`api/app`), ~11k LOC
TS/TSX (`frontend`), 99 route `/v1`, 117 file test; Lite đang chạy `127.0.0.1:8000`, log runtime
sạch. Các lớp nghiệp vụ đã có:

| Lớp | Đã có |
|---|---|
| Tìm kiếm | 19+ adapter keyless-first, RRF, SimHash dedup, tier tên miền, freshness half-life, deep fan-out |
| Trả lời | SSE + citation `[n]`, citation map sớm, claim check lexical, numeric fidelity, follow-ups, thread carry |
| Nghiên cứu | Deep Research IRCoT + coverage gate, STORM outline, Devil's Advocate, council debiasing |
| Tài chính VN | FA đa nguồn, OHLCV thật, sentiment 7 trụ cột, market intel (peers/alerts/events), news-wire |
| Tri thức riêng | Spaces, KG LightRAG-lite, memory theo key, boosts, watchlist poll-on-demand |
| Bản tin | Scanner extractive, Expert Briefing Engine (grounding verifier + council), podcast |
| Tích hợp | `/v1/context` + `/v1/citations/validate` cho LLM ngoài, MCP 12 tool, OpenAPI snapshot |

**Khoảng trống phát hiện được (đối chiếu Perplexity / Exa / Tavily / Kagi / Ground News):**

1. Không có endpoint **kiểm chứng một tuyên bố** (fact-check) — hạ tầng claim-check chỉ chạy
   *bên trong* câu trả lời của chính hệ thống, không phục vụ được câu hỏi "tin này đúng không?".
2. Không có **dòng thời gian sự kiện**: tin tức trả về dạng danh sách xếp theo điểm; không trả lời
   được "chuyện X đã diễn biến thế nào" và "ai đang đưa tin" (độ phủ nguồn).
3. Agent/hệ thống ngoài phải gọi N lần `/v1/search` — không có **batch**.
4. **Thiếu security headers** chuẩn OWASP (nosniff, frame deny, referrer, COOP, permissions,
   CSP cho API) trên toàn bộ response.
5. Test "bom hẹn giờ": `test_rss_enrichment_without_injected_client` hard-code `pubDate`
   16/11/2026 + `freshness_days=7` → **đỏ từ 24/11/2026** trên mọi máy và CI.
6. Không có đặc tả design system máy-đọc-được cho agent thiết kế (DESIGN.md); token theme chỉ
   nằm trong CSS, khối "system dark" có thể lệch khối "dark" mà không ai biết.

## 2. Nguyên tắc nâng cấp (giữ nguyên các quy ước bắt buộc)

- Giants-first: tái dùng `prepare_context`, `SearchPipeline`, SimHash, tiers, citations — **0
  dòng fetch mới**, mọi URL vẫn đi qua SSRF/pinning/budget/admission sẵn có.
- Không ngụy tạo: không có bằng chứng → `insufficient_evidence`; kết quả không có ngày đăng bị
  loại khỏi timeline và **được đếm** (`undated_dropped`), không bao giờ gán ngày đoán.
- 3 Hard Boundary Rules: trọng số rate-limit, tier admission khai báo, cận trên mọi field — có test.
- Deterministic trước, LLM là tuỳ chọn và fail-open.

## 3. Tính năng mới (v4.5.0)

### 3.1 `POST /v1/verify` — Kiểm chứng tuyên bố

Module: `api/app/orchestration/claim_verify.py`, route `api/app/api/v1/trust.py`.

```
claim -> prepare_context (search -> guarded scrape -> chunk -> rerank)
      -> assess_chunk (mỗi chunk): cửa sổ câu tốt nhất theo lexical entailment
           + tín hiệu bác bỏ VI/EN ("bác bỏ", "tin giả", "debunked", "no evidence"...)
           + lật cực phủ định (loại "không chỉ / not only")
           + độ khớp số liệu của tuyên bố
      -> [tuỳ chọn use_llm] 1 lời gọi LLM rẻ gán lại stance trong rào SEC-07 ngẫu nhiên
           (không thêm bằng chứng; "supports" bị hạ nếu số liệu không khớp; lỗi → giữ lexical)
      -> aggregate_verdict: 1 bằng chứng tốt nhất / tên miền × tier_score × relevance
```

| Ngưỡng | Giá trị | Ý nghĩa |
|---|---|---|
| `RELEVANCE_MIN` | 0.45 | dưới ngưỡng: chunk không bàn về tuyên bố → bỏ |
| `SUPPORT_MIN` | 0.62 | entailment tối thiểu để tính "ủng hộ" (và đủ 100% số liệu) |
| `REFUTE_MIN` | 0.50 | tín hiệu bác bỏ phải nằm trong cửa sổ đúng chủ đề |
| `FLIP_MIN` | 0.75 | lật phủ định là bằng chứng yếu → đòi cửa sổ gần như nguyên văn |

Verdict: `supported` / `refuted` / `contested` (bên yếu ≥ 50% bên mạnh) / `insufficient_evidence`.
`confidence = margin × (0.4 + 0.6 × min(1, domains_thắng/3))`; `single_source=true` khi chỉ 1 tên
miền. Search có trả lời nhưng không scrape được → `insufficient_evidence` + `complete=false`;
mọi engine chết → 503 `no_results`. Bounds: claim 8..500 ký tự & ≥3 từ, `k` 3..12,
`max_evidence` ≤12, domains ≤20. Rate-limit **4**, admission tier `verify (8)`.
MCP tool: `verify_claim`.

### 3.2 `POST /v1/timeline` — Dòng thời gian + lăng kính độ phủ

Module: `api/app/orchestration/timeline.py`. Search news-mode trong cửa sổ `days` (1..365) →
lọc STRICT theo `published_at` thật (feed lệch giờ tối đa +6h) → SimHash gộp bản tin
syndication thành 1 sự kiện → ngày sự kiện = **lần đưa tin đầu tiên**, `last_update_at` = bản
mới nhất → giữ `max_events` sự kiện phủ rộng nhất rồi xếp cũ → mới. Lăng kính độ phủ:
`by_domain_tier`, `by_country` (chỉ ccTLD rõ nghĩa; .com/.net → `global`), `volume_by_day`.
Không LLM. Rate-limit **2**, tier `search`. MCP tool: `event_timeline`.

### 3.3 `POST /v1/search/batch`

≤5 truy vấn độc lập/1 round-trip, chạy song song tối đa 3, mỗi item qua pipeline thường
(cache, breaker, `admit("search")`). Partial by design: item lỗi trả `error` riêng, không làm
fail batch. Billing **1 đơn vị/truy vấn** (đếm `queries` trong `_request_item_count`) — batch
không bao giờ rẻ hơn gọi lặp.

### 3.4 Security headers (OWASP Secure Headers)

`SecurityHeadersMiddleware` (pure ASGI, SSE-safe, đăng ký ngoài cùng để cả UI tĩnh cũng có):
`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`
(không lộ URL trang sang nguồn được trích), `Cross-Origin-Opener-Policy: same-origin`,
`Permissions-Policy` (chỉ `microphone=(self)` cho đọc chính tả), và CSP
`default-src 'none'; frame-ancestors 'none'` cho `/v1/*`, `/healthz`, `/readyz`, `/metrics`.
HSTS tắt mặc định (`HSTS_MAX_AGE_S=0`; Lite chạy http loopback) — bật khi sau TLS proxy.
Header do route tự đặt luôn thắng. Tắt toàn bộ: `SECURITY_HEADERS_ENABLED=false`.

### 3.5 Web UI — tab "✅ Kiểm chứng & Dòng thời gian"

`frontend/components/TrustPanel.tsx` + helper thuần `frontend/lib/trust.ts` (có test):
banner verdict (icon + chữ, không chỉ dựa màu), danh sách bằng chứng trích nguyên văn kèm
tier/ngày đăng/lý do (tín hiệu bác bỏ, lật phủ định, % số liệu khớp); timeline có rail +
histogram khối lượng theo ngày + chip độ phủ. Chỉ dùng biến theme; mọi href qua `safeHref()`.

## 4. Đánh giá `google-labs-code/stitch-skills` và quyết định tích hợp

**Là gì:** bộ Agent Skills (Apache-2.0, "not an officially supported Google product") cho
Google Stitch — 3 plugin: `stitch-design` (generate-design, manage-design-system,
extract-design-md, code-to-design…), `stitch-build` (react-components, react-native,
shadcn-ui, remotion…), `stitch-utilities` (design-md, enhance-prompt, taste-design, stitch-loop…).

**Phụ thuộc:** gần như mọi skill **bắt buộc Stitch MCP server** (tài khoản Google, dịch vụ
mạng ngoài), nhiều skill tải asset bằng curl/npx. Chỉ `enhance-prompt` là thuần chỉ dẫn.

| Tiêu chí | Đánh giá |
|---|---|
| Runtime sản phẩm (search/answer API) | ❌ Không phù hợp — đây là công cụ cho agent lập trình, không phải thư viện chạy trong API; đưa vào runtime vi phạm keyless-first và egress tối thiểu |
| Cài plugin vào repo (`npx plugins add …`) | ⚠️ Không tự làm: tải + chạy mã bên thứ ba, cần Stitch MCP + đăng nhập Google của chủ — để chủ tự cài nếu muốn (lệnh ở dưới) |
| Quy ước **DESIGN.md** | ✅ Rất phù hợp — đặc tả design system máy-đọc-được, giúp mọi agent (Stitch, Claude, Cursor) sinh màn hình đúng theme, đúng quy tắc "không hardcode màu" |
| `react-components` / `shadcn-ui` | ❌ Không phù hợp — UI dùng Next.js static export + CSS variables thuần, không Vite/shadcn/Tailwind; chuyển đổi = rewrite lớn, không có lợi ích chức năng |
| `remotion` (video walkthrough) | ⏸️ Tuỳ chọn marketing, không thuộc phạm vi sản phẩm |

**Đã tích hợp (v4.5.0):**

- `frontend/DESIGN.md` theo định dạng Stitch (theme & atmosphere, palette & roles, typography,
  components, layout, do/don't) — bảng token sinh từ `globals.css` thật.
- `frontend/lib/designTokens.test.ts` **khoá DESIGN.md với code**: mọi token có đủ 2 theme, giá
  trị khớp từng byte, khối "system dark" phải trùng khối "dark", và cấm literal màu hex trong
  `components/*.tsx` (trừ fallback SSR của `themeVar()` cho canvas). Đặc tả không thể "trôi".

Muốn dùng Stitch để thiết kế màn hình mới (máy chủ dự án, ngoài repo):

```powershell
npx plugins add google-labs-code/stitch-skills --scope project --target claude-code
```

rồi cấu hình Stitch MCP theo hướng dẫn của Stitch; khi sinh màn hình, trỏ agent vào
`frontend/DESIGN.md`. Không commit thư mục `.stitch/` (asset tải về) nếu chưa review.

## 5. Sửa lỗi đi kèm

- **Test bom hẹn giờ** `tests/test_last30days_search.py::…::test_rss_enrichment_without_injected_client`:
  `pubDate` giờ tính tương đối (`now − 1 ngày`) — đỏ trên mọi máy từ 24/11/2026.
- `search.py`: tách `result_dict()` dùng chung cho `/search` và `/search/batch` (hết lặp shape).

## 6. Đề xuất tính năng unique tiếp theo (chưa triển khai — backlog có thứ tự)

| # | Tính năng | Giá trị riêng | Dựa trên |
|---|---|---|---|
| U1 | **Claim Watch** — theo dõi một tuyên bố, tự kiểm lại khi poll (poll-on-demand như watchlist), báo khi verdict đổi | "Tin đồn này đã được xác nhận/bác bỏ chưa?" — không sản phẩm VN nào có | `/v1/verify` + watchlist storage |
| U2 | **Blindspot lens** — so độ phủ một sự kiện giữa nhóm nguồn (VN chính thống vs quốc tế vs cộng đồng), cảnh báo sự kiện chỉ một phía đưa | Kiểu Ground News nhưng cho thị trường VN | `/v1/timeline` coverage |
| U3 | **Answer diff** — hỏi lại câu cũ sau N ngày, hiển thị câu trả lời đã thay đổi gì và nguồn nào mới | Theo dõi diễn biến thay vì đọc lại toàn bộ | thread carry + answer cache |
| U4 | **Verify-in-answer** — nút "kiểm chứng câu này" trên từng câu của câu trả lời / bản tin | Đóng vòng tin cậy ngay trong UI | `/v1/verify` + `unsupported_markers` |
| U5 | **Disclosure tracker** — dòng thời gian công bố thông tin doanh nghiệp (HOSE/HNX/SSC) gắn với biến động giá | Liên kết sự kiện ↔ giá có bằng chứng | `/v1/market/events` + event-impact |
| U6 | **Source reliability learning** — điểm tin cậy theo tên miền học từ lịch sử verify (tỉ lệ bị bác bỏ), cộng vào tier | Tier tự cải thiện theo dữ liệu thật | verify log + tiers |
| U7 | **Batch verify** cho hệ thống ngoài kiểm chứng câu trả lời LLM của họ theo từng câu | Hàng rào hallucination dạng dịch vụ | `/v1/citations/validate` + verify |

Thứ tự khuyến nghị: U4 → U1 → U2 (dùng ngay hạ tầng v4.5.0, rủi ro thấp), rồi U6/U7.

## 7. Audit loop (tự kiểm — 5 vòng, có thăm dò live)

| Vòng | Cách | Lỗi tìm thấy → sửa (mỗi lỗi có test hồi quy trong `tests/test_v45_trust_layer.py`) |
|---|---|---|
| 1 | từng module + baseline full suite | test bom hẹn giờ `test_rss_enrichment_without_injected_client` (đỏ trên mọi máy từ 24/11) |
| 2 | fresh-eyes đọc lại diff | batch dùng chung 1 budget 5s (item xếp hàng sau semaphore bị cắt) · `max_length` ở field **đầu ra** biến URL dài thành 500 · chunk không dấu câu thành 1 "câu" khổng lồ làm relevance phồng (→ cửa sổ trượt 40 từ) |
| 3 | đối chiếu nghiệp vụ VN | số thập phân VN `7,09%` ≠ EN `7.09%` → tuyên bố tiếng Việt không bao giờ được nguồn quốc tế ủng hộ (→ `canon_number`) |
| 4 | **thăm dò đối kháng live** (engine thật, không đụng Lite :8000) | bài Google News gộp hết thành `news.google.com` → đếm thiếu nguồn độc lập (→ `_effective_domain` theo `source_url`) · "GDP tăng 9,5%" bị ủng hộ vì `95%` khớp **substring** trong `6,95%` (→ so khớp số nguyên token) · "uống thuốc tẩy chữa COVID" bị ủng hộ vì đoạn văn *thuật lại* tin đồn (→ `_HEARSAY_CUES`: "claimed", "tin đồn", "được cho là"… không bao giờ là support, kể cả khi LLM judge gán; thêm cue bác bỏ "myth/misinformation/thông tin sai lệch") |
| 5 | rà lỗi cùng loại ngoài phạm vi mới | **lỗi có sẵn**: `citations.verify_numeric_fidelity` (độ trung thực số liệu của `/v1/answer`) cũng so substring → số bịa "95%" được chấm 100% khi nguồn chỉ có "6,95%"/"1995". Helper dùng chung `numbers_in`/`number_present` ở `app/llm/citations.py` |

Kết quả thăm dò live sau sửa: "GDP Việt Nam năm 2024 tăng 7,09%" → `supported` (7 tên miền độc lập,
gồm `tapchinganhang.gov.vn` tier official); "… tăng 9,5%" → `insufficient_evidence`; "Drinking bleach
cures COVID-19" → `refuted` (cue "myth"/"no evidence"); "The Eiffel Tower is located in Berlin" →
`insufficient_evidence`.

**Giới hạn đã biết (trung thực):** phương pháp từ vựng không suy luận được mâu thuẫn tri thức
("Eiffel ở Berlin" → không bác bỏ, chỉ *chưa đủ bằng chứng*) và không coi một số liệu khác là bác bỏ
(9,5% vs 7,09% → *chưa đủ*, không phải *bác bỏ*). Hệ thống chọn thiên về `insufficient_evidence`
thay vì đoán; `use_llm=true` cho phép LLM gán lại stance (vẫn bị chặn bởi numeric + hearsay guard).
Lens: correctness, honesty, security (SSRF tái dùng, rào SEC-07, CSP), boundary rules, perf
(`run_cpu`, semaphore 3), contract (OpenAPI snapshot regen có chủ đích).
