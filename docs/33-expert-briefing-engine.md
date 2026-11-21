# docs/33 — Expert Briefing Engine: Multi-AI Consensus & Audio-Enabled Intelligence (2026-11-21)

Chuyển đổi từ aggregator tin thô sang **hệ thống bản tin chuyên sâu do AI tổng
hợp, hội đồng phản biện và phát được audio** — theo mandate "Expert-Synthesized,
Multi-AI Consensus & Audio-Enabled Intelligence Briefing System".

## 1. Briefing Channel Registry (single source of truth)

`api/app/orchestration/briefing_registry.py` — `BriefingChannel` dataclass:
mỗi kênh có **cửa sổ thời gian cuộn chính xác** (`window_hours`), **Domain
Expert Persona** (`persona_name` + `persona_rules`), engine categories, tags.
Thêm kênh mới = **1 entry JSON** (file chỉ định qua `BRIEFING_CHANNELS_FILE`)
hoặc 1 entry default — không đổi code:

- **Core news (🗞️ Tin Tức, 6 kênh cửa sổ cuộn)**:
  `macro_economy` 72h (*Chief Macroeconomist*), `markets_stocks` 48h
  (*Senior Equity Strategist & Portfolio Manager*), `business_corporate` 72h
  (*Corporate Strategy & M&A Analyst*), `tech_ai` 72h
  (*Principal AI & Tech Industry Architect*), `banking_fx` 72h
  (*Banking, Credit & Monetary Policy Specialist*), `global_economy` 72h
  (*Global Macro & Geopolitical Strategist*).
- **Deep channels (📰 Blog Chuyên Sâu)**: `ai_coding` 96h (*Staff AI Engineering
  & Coding Agent Architect*), `ai_agents` 96h (*Autonomous Agent Systems &
  Protocol Architect*), `vietnam_stocks` 72h (*Vietnam Market Strategist —
  HOSE/HNX/Foreign Flow*), `macro_economy` (reused), `fengshui_lifestyle` 168h
  (*Classical Eastern Metaphysics & Practical Feng Shui Researcher —
  grounded, analytical, non-superstitious*), `health_wellness` 168h
  (*Evidence-Based Preventive Medicine & Longevity Specialist — strictly
  citing research, never diagnosing*).
- "Tất cả" + "🌐 Bản tin chung" render qua `scan_all`/run digest có sẵn.

Override JSON (mỗi kênh 1 object): `{"channels": [{id, name, kind, window_hours,
persona_name, persona_rules, engine_categories, description, tags, enabled}]}` —
id trùng built-in thì REPLACE (override persona/window); row lỗi bị skip an toàn.

## 2. Deterministic pre-processing (trước LLM — hệ thống trước)

`briefing_engine.collect_channel_evidence`:

1. Fan-out tối đa 6 engines khớp `engine_categories`, truyền
   `freshness_days = ceil(window_hours/24)` (48h→2, 72h→3).
2. **STRICT window filter**: chỉ nhận bài có `published_at` THẬT trong
   `now - window_hours`; bài không có mốc thời gian bị loại (không thể chứng
   minh recency — không đoán). `dropped_stale` báo cáo minh bạch.
3. **Canonical-URL dedup + SimHash event-thread clustering** (hamming ≤3):
   bài syndication gộp thành MỘT thread, `thread_members` giữ mọi URL,
   `engines` hợp nhất đa nguồn.
4. **Authority ranking**: `tier_score(domain)` + bonus đa-engine + decay
   recency 1 tuần → LLM nhận evidence packet đánh số `[n]` có ngày + URL.

## 3. Step A/B — Synthesis + Council Consensus

- **Step A** (`_build_synthesis_prompt`): persona + hợp đồng zero-fabrication
  (chỉ dùng bằng chứng [n]; thiếu bằng chứng phải ghi rõ; KHÔNG kiến thức
  ngoài) + cấu trúc 3 phần: *Bức tranh tổng quan* → *Các diễn biến trọng yếu*
  (What happened → Why it matters → Tác động bậc hai) → *Góc nhìn chuyên gia &
  Điểm cần theo dõi* (catalyst 3-7 ngày).
- **Deterministic grounding verifier** (`_strip_unverified`): mọi `[n]` phải
  trỏ vào packet; **số liệu trong draft phải tồn tại trong text bằng chứng**
  (match digits) — câu vi phạm bị LOẠI trước khi publish, đếm
  `stripped_unverified`. Zero-hallucination được cưỡng chế bởi HỆ THỐNG.
- **Step B council review**: 2 reviewer roles (Fact-Checker, Domain Skeptic)
  qua gateway trusted-only (docs/30 allowlist); verdict JSON
  `{hallucination_free, depth_ok, issues}`; fail → **1 revision pass** với
  issues → verifier chạy lại. Kết quả ghi vào **Dream-RSI policy store**
  (`record_endpoint_outcome` — provider/model/latency/peer score) để
  endpoint selection tiếp tục học (council.py:108).
- **Fallback**: AI tắt/lỗi → extractive briefing từ cùng evidence
  (`ai_grounded=False`, nhãn rõ trong header) — không bao giờ fail-open vào
  nội dung bịa.

## 4. API + Audio

- `GET /v1/briefings/channels` — introspection registry (window/persona/tags).
- `POST /v1/briefings/{channel_id}` — generate ngay (require_admin như scan;
  single-flight per channel; W5 cost 8; response = BlogPostSummary).
- `GET /v1/briefings/{channel_id}/latest` — bản mới nhất đã persist.
- Briefings persist thành **BlogPost** (topic=channel id, tags +`ai-briefing`,
  `council-reviewed`) → tab Blog Chuyên Sâu + `/v1/digests` + RSS đọc tự nhiên.
  **Audio-enabled**: đọc bằng Web Speech ngay trong tab (nút đọc có sẵn),
  và pipeline VoiceStudio (`/v1/podcast/*`) tiêu thụ cùng nguồn BlogPost —
  không cần thêm adapter TTS.

## 5. Config

`BRIEFING_AI_ENABLED=true` · `BRIEFING_REVIEW_ENABLED=true` ·
`BRIEFING_MAX_EVIDENCE=18` · `BRIEFING_CHANNELS_FILE=` (path JSON override).
Frontend: nút `🧠 Tổng hợp chuyên gia` + `📄 Bản tin đã lưu` trong mỗi category
của tab Tin Tức (`NewsPanel.tsx`), render qua `AnswerMarkdown` với citation
chips; `api.ts` thêm `generateBriefing`/`latestBriefing`/`listBriefingChannels`.

## 6. Kiểm định

12 test (`tests/test_briefing_engine.py`): registry shape + JSON override
(thêm kênh không code), STRICT window (loại undated + stale), fan-out freshness
+ thread clustering, grounding verifier ([n] out-of-range + fabricated numbers
stripped), council pass, revision trigger, extractive fallback, empty-evidence
honest, endpoint gating. W5 47 test xanh; contract snapshot regen; full pytest
exit 0; mypy 193 file sạch; frontend tsc + 110 vitest + build xanh.
