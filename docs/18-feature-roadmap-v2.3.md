# 18 — Feature Roadmap v2.3.0: 6 tính năng hoàn chỉnh (plan, solution, checklist)

> Trạng thái: **đang triển khai cuốn chiếu** (implement → tests → audit mini →
> commit per wave → audit tổng kết → push). Đầu vào: 6 đề xuất đã được chủ dự
> án chốt sau audit v2.2.0 (8 vòng expert).

## 0. Nguyên tắc chung (bắt buộc trên mọi wave)

- Giants-first; graceful degradation (tính năng phụ thuộc LLM/VoiceStudio/DB
  chết → trả rỗng/503 có nghĩa, KHÔNG làm sập request khác).
- Không ngụy tạo: mọi số/nến/tin hiển thị phải có `source_url` + `as_of`
  từ payload; thiếu → `null` + nhãn "không rõ".
- Auth: mọi route mới nằm sau `_auth` (router-level); tenancy theo
  `current_api_key_id`.
- CPU nặng không chạy trên event loop; HTTP đi qua pool `app/http.py`.
- Tests cho từng module; `ruff` + `bandit` 0; frontend `typecheck` + `build`
  + vitest xanh trước mỗi commit.

## 1. Wave A — Tương tác giọng nói (frontend thuần)

### A1. Dictation tiếng Việt (nói → truy vấn)

| Mục | Nội dung |
|---|---|
| Solution | `frontend/lib/dictation.ts`: bọc `SpeechRecognition` (webkit-prefixed), `lang="vi-VN"`, interim results, auto-stop khi im ~1.2s, nút mic 3 trạng thái (idle/listening/error); ghí vào ô tìm kiếm chính + ô truy vấn tin tức. Không hỗ trợ → ẩn nút (đúng pattern `ttsReady`). |
| Checklist | [ ] dictation.ts (start/stop/isSupported, guard trình duyệt) · [ ] nút mic search tab + news tab · [ ] a11y aria-pressed/label · [ ] vitest cho guard helpers · [ ] typecheck+build xanh |
| Rủi ro | Chrome-only API (Firefox/Safari cũ không có) → degradation ẩn nút; microphone permission từ chối → hiện hint. |

### A2. Tab Deep Research + TTS theo section

| Mục | Nội dung |
|---|---|
| Bối cảnh | Backend FR-9 đầy đủ (`POST /v1/research` 202 → `GET /v1/research/{id}` → `GET /v1/research/{id}/events` SSE) nhưng **chưa có UI tiêu thụ** — bỏ trống là lãng phí tài năng nhất của hệ thống. |
| Solution | Tab "🔬 Deep Research": form (query, breadth 2-5, depth 1-5, max_time_s) → tạo job → SSE events (stage/token) → render report markdown + learnings + citations. TTS playlist: tách report theo heading `^#{1,3} `, danh sách section bấm-để-nghe, prev/next/dừng, đọc bằng engine hiện tại (Web Speech/VoiceStudio, tái dùng `effectiveEngine` pattern của NewsPanel). |
| Checklist | [ ] api.ts: createResearch/getResearch/streamResearchEvents · [ ] ResearchPanel.tsx (SSE render + progress + citations) · [ ] sectionTTS.ts (split markdown → sections, playlist state machine) · [ ] nút nghe/next/prev · [ ] vitest split logic · [ ] không block tab khác; cancel khi rời tab |
| Rủi ro | SSE dài: abort khi rời tab; report trống/LLM chết → hiện `error` job thật, không bịa. |

## 2. Wave B — So sánh mã + biểu đồ nến (tài chính)

### B1. `POST /v1/fa/compare` + bảng so sánh

| Mục | Nội dung |
|---|---|
| Solution | Endpoint mới `api/app/api/v1/fa.py`: `{symbols: ["VCB","FPT"], ...}` (cap 5, `ensure_symbol`) → fan-out `FAAggregator.fetch_financials` per symbol (gather, timeout chung) → trả `{results: [{symbol, name, exchange, fields: {pe: {value, source, as_of, validated}}, missing_fields, sources_used, as_of}]}`. Aggregator dùng chung qua `deps.get_fa_aggregator()` (lru_cache — breaker sống xuyên request). Frontend: tab "⚖️ So sánh mã": nhập 2-5 mã, bảng metrics × symbols, ô `validated` có tick chéo, footer nguồn + mốc, nút 🔊 đọc bảng (tts.ts). |
| Checklist | [ ] deps.get_fa_aggregator · [ ] fa.py endpoint + schema (cap/symbol guard) · [ ] tests MockTransport (2 mã, 1 mã chết, >5 mã, auth) · [ ] router _auth + weight · [ ] ComparePanel.tsx + bảng + TTS · [ ] typecheck/build/vitest |
| Rủi ro | SSI/TCBS/FireAnt chết từ egress (docs/15 §10) → bảng rỗng có nghĩa + banner partial; không bịa số. |

### B2. `POST /v1/market/ohlcv` + biểu đồ nến

| Mục | Nội dung |
|---|---|
| Solution | Endpoint `api/app/api/v1/fa.py` (cùng file): `{symbol, days?: 30-365}` → OHLCV từ VNDIRECT primary (hàm sẵn) + SSI fallback qua `FAAggregator.fetch_ohlcv`; trả `{symbol, t,o,h,l,c,v, source_name, source_url, as_of}`. Frontend: dep mới `lightweight-charts` (Apache-2.0, TradingView — giants đúng chuẩn); chart trong tab So sánh (panel dưới bảng) + thống kê (giá, % phiên, ATR đã tính sẵn? — chỉ hiện số từ payload). |
| Checklist | [ ] endpoint ohlcv + tests (VNDIRECT 200, VNDIRECT chết → SSI, cả hai chết → 503 có nghĩa) · [ ] transform payload → chart data (vitest) · [ ] ChartPanel.tsx · [ ] npm dep lightweight-charts (khóa version) · [ ] dark theme khớp globals.css |
| Rủi ro | Feed scaled ÷1000 của VNDIRECT — dùng đúng đường `×1000` đã có (probe docs/15 §10); nến NaN bị chặn ở adapter. |

## 3. Wave C — Watchlist + cảnh báo tin

| Mục | Nội dung |
|---|---|
| Solution | Model `WatchlistItem` (db/models.py: key_id, query, label, created_at) — bootstrap tự tạo bảng (create_all + `_add_missing_columns`). Endpoints `api/app/api/v1/watchlist.py`: GET/POST/DELETE `/v1/watchlist` (theo key_id, cap 20 item/key); GET `/v1/watchlist/alerts` = poll-on-demand: chạy search pipeline cho từng item (khi client gọi, background poller tùy chọn `WATCHLIST_POLL_MIN` tắt mặc định), so hash tin mới vs `last_seen` (bảng nhỏ hoặc in-memory LRU theo key). Frontend: panel trong tab Tin tức: thêm/xoá theo dõi, badge "Mới", danh sách tin chưa đọc + 🔊 đọc các tin mới. |
| Checklist | [ ] model + bootstrap column check · [ ] CRUD endpoints + tests (tenancy: key A không thấy của key B) · [ ] alert-hash logic + tests · [ ] poller opt-in config · [ ] UI panel + badge + TTS tin mới |
| Rủi ro | DB chết → in-memory fallback (quy ước graceful); poll chạy qua pipeline chịu admission/budget sẵn có. |

## 4. Wave D — Podcast bản tin (tùy VoiceStudio)

| Mục | Nội dung |
|---|---|
| Solution | `api/app/tts/briefing.py`: preset server-side (mirror docs/16 §4) → gom top tin mỗi mục → script VN → `VoiceStudioClient.synthesize` → file trong `api/podcast_data/` (bounded, retention 7 ngày, tên `briefing-YYYY-MM-DD.mp3`). Endpoints: POST `/v1/podcast/briefing` (tạo thủ công, 202 + poll status), GET `/v1/podcast/briefing/latest` (redirect/stream file), GET `/v1/podcast/briefing/feed.xml` (RSS enclosure). Tất cả 503 `TTS_DISABLED` khi chưa cấu hình. Frontend: nút "🎙️ Tạo bản tin audio" trong tab Tin tức + player + link RSS. |
| Checklist | [ ] briefing.py (script builder, tests cho text không LLM — pure) · [ ] endpoints + tests (disabled→503, file tồn tại→200, RSS XML hợp lệ) · [ ] retention dọn file cũ · [ ] nút UI + player |
| Rủi ro | Không có VoiceStudio → 503 + hướng dẫn; file lớn — cap thời lượng script (tts_max_chars × N chunk đã có). |

## 5. Kế hoạch audit (cuốn chiếu)

| Bước | Nội dung | Tiêu chí |
|---|---|---|
| Mini-audit mỗi wave | Tự review 2 lượt (từng module → fresh-eyes) + gates: pytest/ruff/bandit/typecheck/build/vitest | 0 lỗi, mọi test mới xanh |
| Audit tổng kết (2 expert read-only, đối kháng) | (1) Security/SA: route mới sau auth? SSRF? resource cap? tenancy? (2) QA/data-integrity: provenance, không bịa số, degradation đúng, contract UI↔backend khớp docs/18 | Không P0/P1; P2 phải fix; P3 ghi lộ trình |
| Loop | Fix findings → chạy lại gates → gửi lại expert xác nhận | PASS ×2 expert |
| Chốt | bump 2.3.0 (pyproject/__init__/frontend) + CHANGELOG + AGENTS + roadmap → commit → push | pre-commit 7 gates xanh |

## 6. Trạng thái triển khai

| Wave | Trạng thái |
|---|---|
| A1 Dictation | ✅ (mic ở ô tìm kiếm + ô lọc tin; `lib/dictation.ts` + vitest) |
| A2 Research tab + TTS section | ✅ (tab 🔬 Deep Research: SSE live report + playlist theo section; `ResearchPanel.tsx`, `sectionTTS.ts`) |
| B1 FA Compare | ✅ `POST /v1/fa/compare` (cap 5, fan-out aggregator, provenance mỗi field) + tab ⚖️ So sánh + đọc bảng |
| B2 OHLCV chart | ✅ `POST /v1/market/ohlcv` (VNDIRECT primary, SSI fallback qua aggregator) + `lightweight-charts@5.3.0` (pinned) + thống kê từ payload |
| C Watchlist + alerts | ✅ model `WatchlistItem` + CRUD `/v1/watchlist` + `GET /v1/watchlist/alerts` poll-on-demand (baseline hash) + UI badge/tin mới/đọc; **quyết định thay đổi so với plan**: bỏ background poller `WATCHLIST_POLL_MIN` — poller nền sẽ âm thầm đốt budget search của mọi key; client tự gọi /alerts theo lịch của nó (đề xuất "tốt hơn", xem §7) |
| D Podcast | ✅ `POST /v1/podcast/briefing` (202, idempotent theo ngày) + `/status` + `/latest` (audio/mpeg) + `/feed.xml` (RSS); file `api/podcast_data/briefing-YYYY-MM-DD.mp3`, retention 7 ngày; 503 `TTS_DISABLED` khi chưa có VoiceStudio |
| Audit tổng kết + push | ✅ 3 vòng audit đối kháng (8 lens: finance, technical/SA, AI/LLM, QA, auditor/security, data-engineer, dream-rsi, perf) — xem §8; không còn P0/P1, mọi P2 đã fix, P3 hoặc fix hoặc ghi nhận có chủ đích |

## 7. Đề xuất thay đổi so với plan gốc (đã áp dụng)

1. **Bỏ background poller watchlist** (`WATCHLIST_POLL_MIN`): poller nền chạy
   search pipeline cho mọi key mà không ai yêu cầu → tốn budget/rate-limit và
   có thể làm "tin mới" biến mất trước khi user kịp đọc (baseline tự'avance).
   Thay bằng **poll-on-demand**: client bấm "Kiểm tra tin mới" (hoặc hẹn giờ
   ở client) → `GET /v1/watchlist/alerts` diff với baseline và advance nó.
   Đơn giản hơn, không tốn tài nguyên nền, và semantics rõ ràng.
2. **Podcast một file/ngày, idempotent**: `POST /v1/podcast/briefing` trả về
   bản đã có của ngày thay vì tạo trùng — tránh nhân bản file và tốn GPU/CPU
   TTS vô ích.
3. **Bảng nến dùng field chuẩn của lightweight-charts** (`time/open/high/low/
   close/volume`) thay vì `t/o/h/l/c/v` — UI không cần remap, contract rõ ràng.
4. **RSS feed cũng nằm sau auth** như mọi route /v1 khác (an toàn hơn feed
   public chứa link nội bộ); app podcast dùng header Authorization.
5. **Script podcast bị cap ở sentence boundary** (`podcast_max_chars`) để
   audio không bị cắt giữa từ; script chỉ chứa headline thật từ search, không
   nhờ LLM bịa (giữ đúng quy tắc không ngụy tạo, chi phí = 0 LLM).
6. **Một parser dchart duy nhất cho cả search và chart** (`_parse_dchart_series`
   ở `viet_stock.py`): strict — độ dài 6 mảng phải khớp, mọi giá trị finite
   và không âm, time tăng nghiêm ngặt, `zip(strict)` ở API boundary; feed
   schema drift → từ chối cả series thay vì bịa OHLC/volume. Chart không bao
   giờ hiển thị time bịa (series không có `times` → 503, không fallback
   lịch 86400 giây).
7. **SSE client normalize CRLF trước khi tách frame** (sse-starlette 3.x dùng
   `\r\n\r\n`): cả `streamAnswer` và `streamSse` ở `frontend/lib/api.ts`;
   `streamSse` thêm guard overflow 1MB + `reader.cancel()` trong finally,
   `streamAnswer` nay cũng có cả hai (trước loop 3 chỉ có CRLF).
8. **lightweight-charts v5 API** (`chart.addSeries(CandlestickSeries, …)`,
   pinned `5.3.0`): bản v4 `addCandlestickSeries` đã bị xoá — nếu không pin
   version, upgrade hậu trường sẽ gãy build. `time` cast sang type `Time` của v5.

## 8. Audit loop 3 — phát hiện và fix (2026-11-12)

Loop 3 (fresh-eyes trên toàn bộ diff sau loop 1+2, 8 lens tự verify vì child
limit của agent runner): xác nhận toàn bộ fix loop 1 (39/39) và loop 2 land
đúng (full pytest rc=0, alembic 0006 khớp model 1:1, vitest/tsc/build xanh).
Phát hiện mới ở loop 3 — đều đã fix + có test:

| # | Mức | Vấn đề | Fix |
|---|---|---|---|
| 1 | P2 | `_owner_locks` evict lock đang bị giữ khi map đầy → alias 2 lock cho 1 owner, mất mutual exclusion | evict chỉ entry `not locked()` (`watchlist.py`) + test giữ nguyên object |
| 2 | P3 | `market.py` fallback aggregator raise → 500 thay vì 503 | bọc try/except → `503 no_results`, không leak exception (`test_market_ohlcv_fallback_exception_is_503_not_500`) |
| 3 | P3 | Podcast job bị cancel (shutdown) → `_jobs` mắc kẹt `"running"`, chặn mọi POST sau | `except CancelledError` set `status="cancelled"` rồi re-raise + test |
| 4 | P3 | `_prune_jobs` giữ N+1 ngày còn disk giữ N → status/file có thể disagree | horizon về `retention_days - 1` cho khớp `briefing.py` |
| 5 | P3 | RSS enclosure fallback từ `request.base_url` không check scheme (Host/X-Forwarded giả mạo) | chỉ nhận `http(s)://`, `public_base_url` cấu hình ưu tiên; ngược lại enclosure relative + test `javascript:` không lọt vào feed |
| 6 | P3 | Retention xoá cả `.mp3.tmp` đang được ghi giữa `write_bytes` và `replace` → job báo lỗi giả | chỉ dọn tmp già hơn 1h + test cả hai chiều (orphan cũ bị dọn, tmp mới sống sót) |
| 7 | P3 | `streamAnswer` thiếu guard overflow 1MB và `reader.cancel()` như `streamSse` | thêm cả hai, normalize CRLF trước check overflow |
| 8 | P3 | ResearchPanel: event `done` nhưng GET status còn lag "running" → panel báo lỗi sai | poll lại tối đa 3 lần (400ms) trước khi settle khi đã thấy `done` |
| 9 | P2 | Watchlist migrate về DB khi DB hồi phục bắt đầu từ DB rows → item bị xoá lúc DB down bị "hồi sinh" | memory list là tập id authoritative: `_save_items` upsert memory + xoá phần còn lại ở DB; test `test_watchlist_delete_during_outage_is_not_resurrected` |
| 10 | P3 | `_parse_dchart_series` tính multiplier từ `float(c_list[-1])` trước khi validate → parser có thể raise thay vì trả None | guard try/except + finite/non-negative trước khi tính multiplier + test |
| 11 | P3 | `market/ohlcv` hết wall-clock cả 2 leg → 503 "no data" sai nghĩa | 504 `deadline_exceeded` (cùng contract với fa/compare) + test treo 30s với timeout 0.2s |
| 12 | P3 | `streamSse`/`streamAnswer` chỉ chặn buffer khi KHÔNG có boundary — một frame đơn lẻ >1MB vẫn buffer nguyên | thêm check độ dài từng block sau khi tách frame ở cả hai hàm |
| 13 | P3 | docstring podcast dẫn `docs/18 §7.4` không tồn tại | sửa về §7 item 4 |

Quyết định có chủ đích (chấp nhận rủi ro, đã ghi nhận): `follow_redirects=True`
trên các pool fetch host hằng số (VNDIRECT/SSI/TCBS/FireAnt) — URL không phải do
user cung cấp, symbol đã qua `ensure_symbol`, không gửi credential; per-hop
SSRF re-check chỉ bắt buộc với URL user-supplied theo AGENTS.md §4.
