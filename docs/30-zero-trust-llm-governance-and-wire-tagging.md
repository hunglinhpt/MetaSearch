# docs/30 — Zero-Trust LLM Governance, Wire Entity Tagging & Production Hardening

**Ngày:** 2026-11-19 · **HEAD base:** 65b34f9 (v3.3.1) · **Protocol:** Principal Systems
Architect + AppSec + Quant NLP Lead — 2 expert reviews song song (feature-quality +
security/standardization) → blueprint [P0]-[P3] → cuốn chiếu → 3-pass convergence.

## 1. Blueprint

### [P0] — không có (round 21 sạch P0)

### [P1] Security

| # | Finding | Fix |
|---|---|---|
| P1-1 | **Static fallback chain bypass**: `llm_fallback_provider` / `llm_fallback_chain` / `llm_auto_fallback` (default TRUE) dispatch prompt sang groq/openrouter/mistral/sambanova… không qua sweep lẫn runtime gate (router.py:241-330). Repro: `LLM_PROVIDER=openai` + `GROQ_API_KEY` + 429 → full prompt re-send sang api.groq.com | Runtime gate **mọi** candidate khi `trusted_only` (bỏ static exemption); startup sweep **toàn bộ provider surface** (primary + fallback provider + fallback chain + council endpoints + embedding base_url); fail-closed khi resolution lỗi (prod) |
| P1-2 | **`POST /v1/hybrid` trả full chunk content cross-tenant** (hybrid.py:130-169 — SELECT không owner filter; docs/26 F-11 follow-up) | Alembic 0010: `api_key_id` trên `documents`/`chunks`; persist stamp owner; hybrid filter `owner` + admin bypass; global-index docs |

### [P2] Hardening & Quality

| # | Finding | Fix |
|---|---|---|
| P2-1 | Startup sweep fail-open (`contextlib.suppress` bọc toàn khối — main.py:70-78) | Per-endpoint try/except; prod fail-closed khi endpoint KHÔNG resolve được |
| P2-2 | Council endpoints (`pollinations:*` default) không nằm trong sweep | Sweep `parse_council_endpoint_specs` |
| P2-3 | Yahoo chart fetch không body cap (sentiment_quant.py:807-830) | `MAX_RESPONSE_BYTES` guard như Simplize |
| P2-4 | Podcast `_jobs` / spaces `_graph_jobs` không còn hard cap khi nhiều job running | Hard cap 4× — evict oldest active khi vượt |
| P2-5 | Quant-only payload đưa unverified rumor vào `social_score` (sentiment.py:2141-2172) | Social pillar zero-weight khi quant-only |
| P2-6 | DB health split-brain: `/ops/status` + `/engines/health` đọc DB rows hot path không ghi (engine_store.py:837-856) | Merge memory fallback counters lên DB rows |
| P2-7 | Stage B alias diacritic-sensitive — EN/diacritic-less wire copy recall ~0 (aliases.py:144) | NFKD fold cả aliases lẫn input |
| P2-8 | `_record_db` dead code + CHANGELOG overstatement | Wire record_result → bounded `_record_db` (fail-open) hoặc xoá — quyết định: wire |

### [P3] Polish & Upgrades

| # | Item | Effort |
|---|---|---|
| P3-1 | recent_windows latency p50/p95/p99 + outcome histogram (latency_ms đang discard) | S |
| P3-2 | Wire tagging coverage metric: `tagged_count`/`tagged_ratio` trong response + Prometheus counter | S |
| P3-3 | CSV cell `lstrip()` all-whitespace (NBSP edge) | S |
| P3-4 | Wire simhash near-dup folding (simhash64 đã có, events.py chưa dùng) | M |
| P3-5 | FA field-level TTL cache (docs/15 Wave 3 3.1) — symbol-level, TTL 6h | M |
| P3-6 | Sentiment response cache + `CACHE_EVENTS` kind=`sentiment`/`news_wire` | M |
| P3-7 | Log hygiene: ddg query[:80] → hash; gateway detail[:500] scrub | S |
| P3-8 | docs/30 (file này) + README security section | S |
| P3-9 | Redis LPUSH/LTRIM backing cho sliding window (multi-worker) | Deferred — M, cần Redis design review |
| P3-10 | Direct Reddit/Finnhub adapters cho sentiment | Deferred — L |
| P3-11 | Undated wire items cap + TTLs vào Settings | Deferred — S |

### [Deferred — có lý do]

- `enforce_llm_endpoint_policy` per-URL defense-in-depth trong `_stream_once`: policy đã gate ở `_candidates` (điểm duy nhất xây candidates) — thêm check per-URL là redundant; dead-code claim sai vì hàm vẫn dùng bởi tests + future per-URL path. Đã verify grep.
- Direct Reddit/Finnhub/StockTwits stream adapters: Phase-2 feature (L effort), docs/30 ghi roadmap.
- HybridResponse/SkillListResponse typed models: P3 polish, snapshot regen nặng — vòng sau.

## 2. Verification checklist (3-pass)

- Pass 1: startup sweep chặn fallback chain untrusted (prod); gateway drop candidate untrusted kể cả static chain khi policy trusted_only; hybrid chỉ trả chunk owned (hoặc admin); wire tag "Vinamilk"/"Hoàng Quân Địa ốc"/"Hoa Phat" (ASCII) đúng ticker.
- Pass 2: recent_windows có p50/p95/p99; tagged_ratio trong response; coverage metric counter; fa cache hit giảm fan-out.
- Pass 3: full suite + ruff + mypy(163) + bandit(0) + frontend + contract snapshot từ trạng thái sạch.
