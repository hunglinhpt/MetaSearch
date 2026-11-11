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
| A1 Dictation | ⏳ |
| A2 Research tab + TTS section | ⏳ |
| B1 FA Compare | ⏳ |
| B2 OHLCV chart | ⏳ |
| C Watchlist + alerts | ⏳ |
| D Podcast | ⏳ |
| Audit tổng kết + push | ⏳ |
