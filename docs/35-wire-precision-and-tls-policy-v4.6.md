# 35 — Độ chính xác news-wire (gắn mã, phân loại, ngày) + chính sách TLS cho `/v1/scrape` (v4.6.0)

> Ngày: 2026-11-26 · Nguồn yêu cầu: đề xuất 4 lát M1–M4 của một hệ thống tích hợp bên ngoài
> (đối chiếu số đo sống 23–26/11). Repo này chỉ cung cấp công cụ chung; chính sách lọc/hiển thị
> của bên tiêu thụ nằm ở phía họ. Kiểm định ở đây là **tự kiểm** (phiên này vừa implement vừa audit).

## 0. Tóm tắt

| Lát | Vấn đề đo được (4.5.0, live) | Đã làm | Nghiệm thu (test thuần, không mạng) |
|---|---|---|---|
| M1 | `HCM` cho mọi tin "TP.HCM"; `TTN` cho "Ngày **truyền thống**"; `VNI` từ "bất động sản"; `CMC`+`CMG` cho tin CMG; 328 alias tự sinh là từ phổ thông/địa danh ("hải phòng"→HAC, "dầu khí"→PSI, "thăng long"→TIG); alias tuyển chọn sai "VinFast"→VFS (VFS là CK Nhất Việt) | geo guard, mã chỉ số, alias 4 chốt, bằng chứng từng mã | bộ vàng 97 tiêu đề: **P = 1.00, R = 1.00**, 0 `HCM` cho tin chỉ có TP.HCM (11 dòng), 0 mã chỉ số trong `symbols` |
| M2 | `regulatory` cho "TP.HCM thu hồi đất", "Iran… cảnh báo thủy lôi", "Quốc hội chốt trần phạt"; IPO Mỹ thành `listing_status`; "thoái vốn công ty con" thành `insider_trade` | luật có thứ tự + `category_reason` + `category_confidence`, scope `global` | bộ vàng 97 tin (mỗi lớp ≥ 8): **macro-F1 = 0.989**, precision `regulatory` = **1.00** |
| M3 | 3/60 tin không có ngày (trang HTML danh sách) | bóc ngày từ trang bài: meta → JSON-LD → URL; `published_at_source` | live: 1/60 không ngày (**98,3 %** có ngày), 100 % có offset |
| M4 | lỗi chứng chỉ TLS bị leo thang sang crawl4ai/Jina, client nhận nội dung không có dấu hiệu | `tls_policy.py`, mặc định từ chối, opt-in `allow_insecure_escalation`, `tls_verified` | máy chủ HTTPS tự ký cục bộ: 0 lần gọi crawl4ai/Jina, 502 `tls_verify_failed`; EOF vẫn leo thang |

## 1. M1 — Gắn mã có bằng chứng

Module mới (mỗi module có test riêng): `api/app/retrieval/symbols/`

- `geo_guard.mask_geo` — che "TP.HCM / TP HCM / TPHCM / Tp.HCM / TP. Hồ Chí Minh / thành phố Hồ Chí Minh /
  HCMC" bằng khoảng trắng **cùng độ dài** (offset không lệch) trước bước bare/phrase. `HCM` chỉ còn
  gắn qua `HCM:`, `HOSE: HCM`, "cổ phiếu HCM", alias "Chứng khoán HSC"/"HSC".
- `index_codes` — `INDEX_CODES = {VNI, VNINDEX, VN30, VN100, HNX, HNXINDEX, HNX30, UPCOM, UPCOMINDEX}`
  không bao giờ vào `symbols` qua bare/alias; bài nói về chỉ số trả `indices: ["VNINDEX", …]`.
  Mã niêm yết trùng mã chỉ số (VNI/UPCOM) chỉ gắn với bằng chứng tường minh (`UPCOM: VNI`).
- `evidence.SymbolEvidence` — mỗi mã một bản ghi (bằng chứng mạnh nhất):
  `{"symbol","method","span","start","field","alias_confidence"?}`;
  `method ∈ prefix | exchange | cashtag | phrase | bare | alias | caller` (`caller` = mã do client cung cấp
  trong `raw_articles`). `start` là offset trong `field` (`title` | `snippet`, chuẩn NFC).

`api/app/retrieval/aliases.py` — 4 chốt độ chính xác:

1. **Generic-phrase guard**: alias tự sinh chỉ gồm từ ngành + địa danh bị loại khi dựng index
   (`is_generic_alias`): "Bất động sản", "Hải Phòng", "Truyền thông", "Cấp nước Hải Phòng".
2. **Khớp đúng dấu**: so khớp trên văn bản bỏ dấu (vẫn bắt được bản tiếng Anh "Hoa Phat"), nhưng span
   có dấu phải trùng một cách viết gốc — "truyền thống" ≠ "Truyền thông". Hai cách đặt dấu hợp lệ
   ("Hoá"/"Hóa", "Thuý"/"Thúy") được coi là một.
3. **Viết hoa tên riêng** (alias tự sinh): mọi âm tiết đặc trưng phải viết hoa.
4. **Acronym phân biệt hoa thường**: alias tuyển chọn ≤ 5 ký tự viết hoa ("HSC") chỉ khớp chữ hoa.

Bare token **nằm trong** span alias thuộc về alias đó ("Tập đoàn Công nghệ CMC" → `CMG`, không thêm `CMC`).
`alias_confidence`: tuyển chọn 0.97; tự sinh 0.90 (+0.05 khi có token hiếm, df ≤ 3); span không dấu
của tên có dấu −0.08. Alias tuyển chọn mới: SeABank→SSB, Novaland→NVL, (TTC) AgriS→SBT, Bảo Minh→BMI,
Chứng khoán Bảo Minh→BMS, Chứng khoán Nhất Việt→VFS, Đá Núi Nhỏ→NNC, Đá Hóa An→DHA, Xuân Mai Corp→XMC,
Thaiholdings→THD, Công nghệ CMC→CMG, Cen Land→CRE, Chứng khoán HSC/HSC→HCM. **Đã xoá** "VinFast"→VFS.

Gợi ý chính sách cho bên tiêu thụ (không áp trong repo): chỉ nhận
`method ∈ {prefix, exchange, cashtag, phrase, caller}` cộng `alias` có `alias_confidence ≥ 0.9`.
`symbols` giữ nguyên kiểu (tương thích ngược). Resolver alias dùng chung với sentiment engine, nên
sentiment cũng hưởng các chốt trên.

## 2. M2 — Phân loại có lý do

`api/app/retrieval/news_category/rules.py` (từ vựng + predicate thuần) +
`events.decide_news_category` → `CategoryDecision(scope, category, severity, reason, confidence)`;
`classify_news_scope_and_category` giữ chữ ký cũ (trả bộ ba).

Thứ tự luật:

1. `regulatory`: **từ chế tài** (xử phạt, bị phạt, truy thu, cưỡng chế, đình chỉ, hạn chế giao dịch,
   diện kiểm soát/cảnh báo, cắt margin, khởi tố, tạm giam, vi phạm, sai phạm, thu hồi giấy phép…)
   **VÀ chủ thể** (mã đã gắn, hoặc cơ quan/sàn có tên: UBCKNN, HOSE/HNX/UPCOM, Thanh tra, Cục thuế…);
   "cảnh báo"/"nhắc nhở" chỉ tính khi đi cùng sàn/UBCKNN. "thu hồi" đơn lẻ (thu hồi đất) không tính.
   Confidence 0.95 (có mã) / 0.85 (chỉ cơ quan).
2. `listing_status`: cụm niêm yết + chủ thể sàn VN; tin nước ngoài → `general_finance`, scope `global`.
3. `macro_policy` (văn bản pháp quy trong **tiêu đề**, khi không có mã): Quốc hội, nghị định, thông tư,
   luật, dự thảo, Chính phủ, ban hành, NHNN…
4. `corporate_action`: cổ tức/chốt quyền/ĐHĐCĐ/cổ phiếu quỹ…; phát hành-chào bán cổ phiếu/trái phiếu,
   tăng vốn, tất toán/mua lại trái phiếu **khi có mã hoặc chủ thể doanh nghiệp**; giao dịch **công ty con**
   (thoái vốn, bán, thành lập) — không còn là `insider_trade`.
5. `insider_trade`: actor nội bộ/cổ đông lớn/quỹ **nêu trong tiêu đề** (bỏ "chứng chỉ quỹ", "cổ phiếu
   quỹ") + chiều giao dịch xác định.
6. `macro_policy` / `market_flow` theo từ khoá tiêu đề (dòng tiền, khối ngoại, thanh khoản, VN-Index…,
   không còn khớp mọi chữ "thị trường"); còn lại `general_finance` (0.5) hoặc gợi ý nguồn (0.55).

Scope mới `"global"` (thêm vào enum `scope` của `MarketNewsWireItemOut`/`MarketNewsRawInput`).

## 3. M3 — Ngày xuất bản

`api/app/retrieval/news_dates.py` (thuần): `published_from_html` (meta `article:published_time` & tương
đương, `<time itemprop=datePublished>`, JSON-LD `datePublished` kể cả `@graph`), `published_from_url`
(`/YYYY/MM/DD/`, `/YYYY-MM-DD/`, `-YYYYMMDD…`, độ chính xác ngày, 00:00 giờ VN), `parse_published`
(giá trị không offset của trang VN = giờ VN). Hợp lệ: 2000-01-01 ≤ t ≤ now + 1 ngày.

`events._enrich_missing_dates`: chỉ chạy cho item **không có** `published_at` lẫn `event_date`; ≤ 12 lần
tải trang, 4 song song, theo ngân sách còn lại của request, qua `_fetch_source_text` (SSRF + IP pinning +
breaker riêng `article_date:<source>`); ghi nhớ LRU 4096 URL (ngày không đổi; "không tìm thấy" nhớ 1 giờ);
chạy **trước** khi ghi cache nên bundle cache mang ngày luôn. Không bao giờ suy từ thời điểm cào.

Hợp đồng: `published_at` luôn ISO-8601 có offset (UTC `+00:00`, đã như vậy từ trước — feed `+0700` được
quy về UTC đúng); `published_at_source ∈ feed | caller | article_meta | json_ld | url | null`.
`date_status` giữ **một** giá trị cho tin không ngày: `"undated"` (không đổi tên thành `missing` để
không phá client hiện có; không bao giờ là chuỗi rỗng).

## 4. M4 — TLS hỏng thì dừng

`api/app/extraction/tls_policy.py`: `is_tls_verification_error(exc)` lần theo `__cause__/__context__`
tìm `ssl.SSLCertVerificationError` (dự phòng chuỗi `CERTIFICATE_VERIFY_FAILED`); `SSLEOFError` /
`UNEXPECTED_EOF` là rớt mạng → vẫn leo thang như cũ.

`scraper.py`: lỗi chứng chỉ ở mọi IP đã xác thực → `TLSVerificationFailed` → `PageFailure(error=
"tls_verify_failed")`, **không** gọi crawl4ai/Jina, **không** negative-cache. Áp dụng cho mọi đường
(`/v1/scrape`, answer, research, crawl). Opt-in `allow_insecure_escalation=true` (chỉ `/v1/scrape`):
leo thang, trang mang `insecure_escalation=True`, **không bao giờ cache** (caller mặc định sau đó vẫn bị
từ chối); single-flight tách theo chế độ.

`/v1/scrape`: mọi item có `tls_verified` (true = HTTPS mà chứng chỉ xác thực được; false = http thường
hoặc nội dung leo thang qua chứng chỉ hỏng). Khi **mọi** URL bị từ chối vì chứng chỉ → HTTP **502**
`tls_verify_failed`; batch hỗn hợp vẫn 200 với lỗi từng item (partial by design).

## 5. Thay đổi hợp đồng (chỉ thêm)

| Schema | Field mới |
|---|---|
| `ScrapeRequest` | `allow_insecure_escalation: bool = false` |
| `ScrapeItem` | `tls_verified: bool` (vắng mặt ở item lỗi) |
| `MarketNewsWireItemOut` | `symbol_evidence[]`, `indices[]`, `category_reason`, `category_confidence`, `published_at_source`; enum `scope` thêm `global` |
| `MarketNewsRawInput` | enum `scope` thêm `global` |
| `ErrorCode` | `tls_verify_failed` → 502 |

Cache news-wire đổi khoá `news_wire:v2` → `v3` (tagging/phân loại đổi, bản cũ không được phục vụ lại).

## 6. Kiểm định

- Test mới: `tests/test_v46_tls_policy.py` (máy chủ HTTPS tự ký thật, spy crawl4ai/Jina),
  `tests/test_v46_wire_precision.py` (bộ vàng + module), fixtures
  `tests/fixtures/vn_headlines_symbols_gold.jsonl` (97), `vn_headlines_category_gold.jsonl` (97).
  Nhãn gán tay; cột `origin` ghi rõ `live_2026-11-26` / `proposal_2026-11-23` / `constructed`.
- Audit tự kiểm bắt và sửa trong phiên: regex `\bcp\b` bị patch ghi thành ký tự backspace (mất nhánh
  "cp" của luật phát hành); actor "quỹ" khớp "chứng chỉ quỹ"/ETF; alias "Petrolimex"/"Sơn Hà" bỏ vì
  va chạm (PGC/PLC; địa danh).
- Còn lại (trung thực): 1/97 lỗi phân loại ("VIFC-HCMC mở 'luồng xanh'…" → general thay vì macro); bộ
  vàng gắn mã chấm trên **tiêu đề** (trích đoạn thêm mã chưa được chấm tay); recall alias cho tên
  công ty tiện ích kiểu "Cấp nước <tỉnh>" giảm có chủ đích (ưu tiên precision).
- Chạy lại kiểm TLS phía bên tiêu thụ (badssl/máy chủ tự ký) khi triển khai bản này.
