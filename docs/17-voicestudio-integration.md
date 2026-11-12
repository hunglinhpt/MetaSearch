> Mục tiêu: đánh giá và tích hợp [debpalash/VoiceStudio](https://github.com/debpalash/VoiceStudio)
> làm **engine đọc máy chủ-side tuỳ chọn** cho tab Tin tức (và câu trả lời), giữ Web Speech API
> làm mặc định; đồng thời đề xuất các chức năng mở rộng khác của VoiceStudio nếu ưu việt hơn
> (nếu ưu việt hơn phần TTS hiện tại). Trạng thái: **đã release v2.2.0 (TTS server-side)**; các tính năng
> mở rộng (dictation, tải bản tin audio) **đã làm ở v2.3.0** — xem §6 (dictation dùng Web Speech API
> của trình duyệt, podcast bản tin dùng chính VoiceStudio).

## 1. VoiceStudio là gì (nguồn: README + docs chính thức, 2026-11)

- **AGPL-3.0**, "fully-local ElevenLabs alternative": voice cloning, voice design, video
  dubbing, dictation, transcription, narrate/audiobook — **646 ngôn ngữ**, chạy on-device
  (Electron desktop, iOS/Android, headless server/Docker `ghcr.io/debpalash/voicestudio:stable`).
- **Local speech platform** trên `:3900`: OpenAI-compatible `POST /v1/audio/speech` (TTS),
  `POST /v1/audio/transcriptions` + WebSocket streaming (ASR), MCP, discovery
  `/.well-known/voicestudio-speech`, bearer `OMNIVOICE_API_KEY` cho remote.
- Engine TTS mặc định **OmniVoice (k2-fsa, 0.6B zero-shot, 600+ ngôn ngữ — có tiếng Việt)**,
  bản GGUF tăng tốc Vulkan/CUDA/MPS/ROCm, tự offload CPU khi < 8 GB VRAM; các engine khác:
  CosyVoice GGUF, Qwen3-TTS, VoxCPM, Chatterbox Multilingual, Kokoro, Kitten TTS (~79 MB,
  chỉ tiếng Anh). Model tải về nhiều GB (xem volume `omnivoice_data`).
- Pro (closed-source, commercial) và Cloud (hosted, chưa ra mắt) — **không dùng**.

## 2. So sánh với TTS hiện tại (Web Speech API, docs/16)

| Tiêu chí | Web Speech API (hiện tại, mặc định) | VoiceStudio server-side (mới, tuỳ chọn) |
|---|---|---|
| Chất giọng tiếng Việt | Tuỳ OS/Edge: local OS hoặc neural online của Edge | OmniVoice neural 0.6B — tự nhiên hơn giọng OS mặc định, **offline 100%** |
| Deps / tài nguyên | 0 — voice stack của trình duyệt | Service riêng + model nhiều GB + CPU/GPU; **không tương thích Lite profile (~100MB)** |
| Egress / riêng tư | Giọng local của OS ở trên máy; giọng online của Edge gửi text đọc tới Microsoft (UI đã công khai) | Text gửi tới máy chủ nội bộ `VOICESTUDIO_URL` (compose: `voicestudio:3900`), **không ra Internet**; UI công khai theo engine đang dùng |
| Chi phí | $0 | $0 (AGPL app; model weights có license riêng từng model) |
| Độ trễ | ~0 (local) hoặc round-trip Edge online | HTTP tới server + thời gian suy luận model (CPU có thể vài giây/câu ngắn) |
| License | — | App AGPL-3.0; tích hợp **chỉ qua HTTP tới process riêng** (aggregation, không import code) nên không lan license sang codebase GPL-3.0-or-later — đã ghi chú, cần legal review nếu đổi hình thức (xem §7) |
| Dung lượng đĩa | 0 | Volume `voicestudio-data` (model nhiều GB, tải lười khi dùng lần đầu) |

**Quyết định (ADR-style)**: giữ **Web Speech API làm mặc định** (phù hợp Lite, zero-dep,
free-first theo ADR-007); thêm **VoiceStudio làm engine tuỳ chọn server-side** ở full profile,
bật bằng `VOICESTUDIO_URL`. Khi chưa cấu hình hoặc upstream lỗi, UI **tự fallback** về Web
Speech (graceful degradation — quy ước bắt buộc của repo).

## 3. Kiến trúc đã triển khai

```
Web UI (NewsPanel / nút đọc câu trả lời)
  │  engine = "webspeech" (mặc định, localStorage)  →  Web Speech API (ttsSpeak)
  │  engine = "voicestudio" (chọn trên toolbar)     →  POST /v1/tts/synthesize (chunk ≤220 ký tự)
  ▼                                                        │ audio/mpeg (ETag + Cache-Control, hỗ trợ 304)
FastAPI /v1/tts/synthesize ──HTTP (pool "tts" (rieng), timeout, cap 8MB)──▶ VoiceStudio :3900/v1/audio/speech
GET /v1/tts/capabilities → { voicestudio: { enabled, model, voice } }   (không lộ URL/key)
```

- Backend: `app/tts/voicestudio.py` (`VoiceStudioClient`, URL **chỉ từ env**, validate scheme
  http/https + không userinfo/query; stream đọc có cap `tts_max_audio_bytes`; lỗi rõ ràng:
  `tts_disabled` 503, `tts_upstream_error` 502, timeout 504). Route `app/api/v1/tts.py`
  (`GET /v1/tts/capabilities`, `POST /v1/tts/synthesize` với ETag/304). Config mới ở
  `app/config.py`: `VOICESTUDIO_URL/API_KEY/MODEL/VOICE/TIMEOUT_S`, `TTS_MAX_CHARS`,
  `TTS_MAX_AUDIO_BYTES`. OpenAPI snapshot đã regen (`METASEARCH_UPDATE_OPENAPI=1`).
- Frontend: `lib/api.ts` (`ttsCapabilities`, `ttsSynthesize` → Blob), `lib/tts.ts`
  (`TtsEngine`, `get/setPreferredEngine` — localStorage, `ttsSpeakRemote`: strip + chunk như
  đường trình duyệt, gọi tuần tự từng chunk, phát bằng một `<audio>` duy nhất, generation
  guard + cancel cả fetch lẫn audio). `NewsPanel` có menu chọn engine ở toolbar (option
  VoiceStudio bị disable nếu backend báo chưa cấu hình); nút đọc câu trả lời ở `page.tsx`
  dùng cùng engine đã chọn. Ghi chú riêng tư trên tooltip đổi theo engine thực dùng.
- Deploy: `deploy/docker-compose.yml` thêm service `voicestudio` dưới `profiles: ["tts"]`
  (không chạy mặc định), port `127.0.0.1:3900`, volume `voicestudio-data`; `.env.example`
  thêm `VOICESTUDIO_URL/API_KEY/MODEL/VOICE` (để trống = tắt).
- Test: `api/tests/test_tts_voicestudio.py` (30 case: payload OpenAI-compatible + bearer,
  URL validate, cap audio, timeout 504, upstream 502/503, route 200/304/400/502/503);
  frontend vitest lên 21 (engine preference + chuỗi chunk remote + cancel + onError).

## 4. Cách bật (full profile)

```bash
docker compose --profile tts up -d voicestudio     # tải model nhiều GB
# .env: VOICESTUDIO_URL=http://voicestudio:3900  (+ OMNIVOICE_API_KEY nếu remote)
docker compose up -d api frontend
# Web UI → tab Tin tức → toolbar "Giọng đọc" chọn "VoiceStudio (server)"
```

Chạy local không Docker: cài VoiceStudio trên host, `VOICESTUDIO_URL=http://127.0.0.1:3900`.
Lite profile: **không hỗ trợ** (quá nặng) — UI tự ẩn tuỳ chọn khi `capabilities.voicestudio.enabled=false`.

## 5. Bảo mật

- URL đích chỉ từ env (`VOICESTUDIO_URL`), request không ảnh hưởng được → không SSRF qua route.
- API key của VoiceStudio chỉ ở server, không bao giờ xuống client (`capabilities` không trả key/URL).
- Cap kích thước audio (`tts_max_audio_bytes`) khi stream để upstream lỗi không ngốn RAM;
  cap text (`tts_max_chars`); timeout riêng (`voicestudio_timeout_s`).
- Port 3900 publish `127.0.0.1` trong compose; remote cần HTTPS/WSS + bearer theo docs của VoiceStudio.

## 6. Các chức năng khác của VoiceStudio — đề xuất (cập nhật v2.3.0: 2 mục đầu đã làm)

| Chức năng | Phù hợp với MetaSearch? | Đề xuất |
|---|---|---|
| **Dictation / ASR streaming** vào ô tìm kiếm & ô tin riêng | ✅ Rất phù hợp — nhập truy vấn tiếng Việt bằng giọng nói | **ĐÃ LÀM v2.3.0** theo hướng an toàn hơn: Web Speech API của trình duyệt (`frontend/lib/dictation.ts`, mic ở ô search + ô lọc tin, vi-VN, không egress backend). VoiceStudio ASR server-side vẫn là tuỳ chọn phase sau nếu cần ASR chất lượng cao hơn mà không phụ thuộc trình duyệt |
| **Narrate & batch → tải cả bản tin thành file audio (mp3)** | ✅ Phù hợp — "tải bản tin về nghe offline/share" | **ĐÃ LÀM v2.3.0**: podcast bản tin server-side qua VoiceStudio (`POST /v1/podcast/briefing` → file mp3 trong `api/podcast_data/`, retention 7 ngày, RSS `/v1/podcast/briefing/feed.xml` + player ở tab Tin tức) — đúng use-case này, không cần ghép chunk client-side |
| Transcribe audio/video URL | ⚠️ Ít liên quan (news là text) | Không làm |
| Video dubbing | ❌ Không có video trong sản phẩm | Không làm |
| Voice cloning / voice design | ❌ Rủi ro consent/giả mạo, trái chính sách riêng tư hiện tại | **Không làm** |
| VoiceStudio Cloud / Pro | ❌ Cloud chưa ra mắt; Pro closed-source + account | Không dùng |

## 7. Rủi ro & ghi chú pháp lý

- **AGPL-3.0**: app của VoiceStudio là AGPL; model weights/voices có license riêng (xem Model
  Manager). Ta chỉ gọi HTTP tới process riêng (mere aggregation) nên codebase GPL-3.0-or-later
  không bị đổi license; **nếu về sau nhúng code/import trực tiếp thì phải legal review lại**.
  Lưu ý AGPL §13: người vận hành một service AGPL qua mạng phải offer Corresponding Source cho
  người dùng remote — ở đây thỏa vì compose bind `127.0.0.1:3900` (nội bộ); **nếu từng expose
  VoiceStudio ra ngoài thì phải bổ sung cơ chế offer source**.
- Model nhiều GB tải lười → lần đọc đầu tiên chậm; UI nên báo "đang tải model lần đầu".
- Port 3900 có thể trùng với service khác của host — có thể đổi qua env của VoiceStudio.
- Lite profile không chạy được VoiceStudio (RAM/đĩa) — UI đã tự ẩn.

## 8. Kiểm thử & audit

- Backend 30 test mới (`test_tts_voicestudio.py`), contract snapshot regen (additive only); frontend vitest 21.
- **Audit đối kháng 2 vòng (mỗi vòng 3 expert: perf / quality / data-sec)**: vòng 1 phát hiện
  2 P1 (pool chung `internal` bị TTS chiếm dụng; scalar timeout đè pool-acquire timeout)
  + các P2/P3 → fix hết (pool `tts` riêng, per-phase timeout, wall-clock deadline, cap error
  body, media whitelist, admission tier + single-flight + LRU, `Cache-Control: private`,
  compose `mem_limit`, prefetch + blob cache + revoke URL + `ttsStop` dừng remote ở frontend).
  Vòng 2 xác nhận toàn bộ FIXED với evidence file:line, verdict **PASS ×3**, không P1/P2 mới.
  Kết quả chi tiết ghi ở CHANGELOG mục [2.2.0].

**Vận hành (audit vòng 8):**
- **Host size**: `mem_limit: 8g` của voicestudio là *cap* chứ không phải phân bổ,
  nhưng model nhiều GB + CPU/GPU synthesize đồng hành với postgres/redis/api —
  chỉ bật profile `tts` trên host ≥ 16GB RAM; host 4-8GB dùng Web Speech API
  (mặc định) hoặc chạy VoiceStudio trên máy khác và trỏ `VOICESTUDIO_URL`.
- **Safari**: chính sách transient-activation có thể từ chối `play()` của chunk
  thứ 2 trở đi trong chuỗi remote (lỗi `audio-play` được báo thẳng, không bỏ
  im lặng); Chrome/Edge không ảnh hưởng.
- **Capabilities cache 1 lần/trang**: thêm/xoá `VOICESTUDIO_URL` giữa chừng chỉ
  có hiệu lực sau khi reload trang (fetch lại khi lỗi là có, thành công thì
  không tự re-validate).
