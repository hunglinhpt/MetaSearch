# 31 — v4.0 Comprehensive Upgrade: Search, Sentiment v3, Deep Research v2 & Eval Suite

**Release target**: `v4.0.0` (2026-11-19)  
**Scope**: Full-stack architectural audit & upgrade across Search/Retrieval, Omni-Channel Bilingual Equity Sentiment, Deep Research & LLM Council, and Offline/Online Evaluation Suites — preserving 100% zero-fabrication guarantees and backward-compatible `/v1` contracts.

---

## 1. Wave 1 — Search & Retrieval Core Overhaul

| Component | Upgrade Implemented | Files |
|---|---|---|
| Keyless adapters & registry | Registered 3 new keyless financial/news connectors (`bing_news`, `vn_wire`, `fin_rss`) in `registry.py` and `engine_store.py`; fixed trailing `IndentationError` in `ddg.py`. | `api/app/retrieval/adapters/`, `registry.py`, `engine_store.py` |
| Bilingual scoring & authority | Added `tokenize_dual()`, `compute_batch_idf()`, bigram + accented relevance scoring, `consensus_score(1, *) == 0.0`, balanced `WEIGHTS`, soft chunk decay + heading boost in `heuristic_chunk_score()`. Expanded `tiers.py` with `"low_trust": 0.25` and `.gov*`/`.edu*` suffix matching. | `api/app/retrieval/scoring.py`, `tiers.py` |
| Fusion, wrapper dedup & relevance gate | Resolved wrapper links (`news.google.com`, `bing.com`) via `_effective_domain`, normalized per-adapter rank offsets in RRF, passed batch IDF, added `0.45x` multi-term relevance gate, Title Jaccard `>= 0.72` near-duplicate folding, and consensus re-sort. | `api/app/retrieval/merger.py` |
| Adaptive query routing & expansion | Expanded `_DEEP_MARKERS`, added `extract_implicit_freshness_days()`, upgraded bilingual `_EXPAND_SYSTEM` hints. | `api/app/retrieval/router.py`, `query_expansion.py` |
| Reranking & extraction | Upgraded default `HeuristicReranker` to hybrid BM25 + heuristic + MMR with `max_per_url=4` and 24-chunk cap; added ONNX 512-token truncation, nested `BGEReranker._pick_file`, and optional `LLMListwiseReranker` (`RERANKER=llm`). | `api/app/retrieval/reranker.py`, `config.py`, `scraper.py`, `chunker.py` |
| Verification | Verified across 12 search/retrieval test files (~301 tests passing offline). | `api/tests/test_v40_search_upgrade.py` |

---

## 2. Wave 2 — Institutional Sentiment Engine v3

- **Verified Keyless Market & Macro Connectors (`sentiment_quant.py`)**:
  - Global feeds: `fetch_cnn_fear_greed` (CNN composite + sub-indicators), `fetch_cboe_daily_pcr` (Total/Equity/Index Put-Call ratios), `fetch_fred_hy_oas` (ICE BofA US High Yield OAS), `fetch_live_market_proxies` (`^VIX` + `^VIX3M`).
  - Vietnam feeds: `fetch_cafef_vn_flows` (foreign/proprietary net value + insider MSPR), `fetch_ssi_iboard_orderflow` (active buy ratio + foreign room), `fetch_ssi_hose_breadth` (Arms TRIN & McClellan RANA), `fetch_dnse_ohlcv_series` (OHLCV fallback), `fetch_vn_benchmark`, and `fetch_simplize_extended` (broker consensus + direct news).
  - Fixed OHLCV down-day volume-surge polarity sign and enforced >= 2 independent signal groups for `bofa_bull_bear_analog_0_10`; added `return_5d_pct`, `return_5d_z`, `return_20d_z`.
- **NLP v3 & Persistence (`sentiment.py`, `0011_sentiment_snapshots.py`, `market.py`)**:
  - 350+ curated VI/EN financial phrases, `_RUMOR_DENIAL_PHRASES` & extended `_RUMOR_PATTERNS`, anti-collision rules for `VNM` (travel/food) and `GAS` (LPG retail), Google News wrapper `source_url` resolution, SEntFiN sentence-level target attribution + `tanh` saturation, SimHash seeding dedup, `BEARISH_MARKDOWN`/`DISTRIBUTION_PRESSURE` regimes, hybrid text+quant blending, opt-in `run_llm_sentiment_judge`, and PostgreSQL/SQLite `sentiment_snapshots` (Alembic `0011`) powering 20-day S-Score / SV-Score calculation.
- **API & UI (`api/v1/market.py`, `SentimentPanel.tsx`)**:
  - Accepted global share/crypto notations (`BRK.B`, `BTC-USD`), exposed `use_llm_judge` & `include_institutional_feeds`, added 180s TTL `_SENTIMENT_CACHE` + `SingleFlight`, excluded synthetic `viet_stock` hits, and rendered Institutional Flows, HOSE Breadth, Analyst Consensus, Corporate Events, and 20d S-Score panels in Web UI.

---

## 3. Wave 3 — Deep Research v2 & LLM Council Debiasing

- **P0 Fixes & Pipeline Resilience (`research.py`)**:
  - Raised `_MAX_CHUNKS_PER_DOC_IN_MERGE` to `6`; applied `fold_diacritics` in `detect_profile()` and `_plan_coverage()`; tracked `executed_queries` to eliminate duplicate gap hops while preserving multi-iteration coverage probes; pre-filtered `seen_urls` before `scrape_many(max_pages=6, timeout_s=8.0)`; wrapped runs with `admit("research")` + `RequestBudget`.
  - Multi-batch character-bounded `_extract_with_conflicts` enforces strict per-batch source ID validation (`citation_id in batch_ids`) and surfaces conflicting claims.
- **Stage 0 Tool Mesh, STORM Outline, Devil's Advocate & FACT Verification (`research.py`)**:
  - Stage 0 concurrently invokes Spaces hybrid RAG + Knowledge Graph (`space=<slug>`) or financial live tools (`fetch_market_news_wire`, `fetch_market_events`, `fetch_sentiment_quant_context`), emitting `tool_results`.
  - STORM planner emits `outline` (`perspectives` + `sections`, deterministic fail-open); Devil's Advocate appends risk/counter-thesis sub-queries on iteration >= 1; final synthesis performs global reranking, contiguous `[1]..[M]` citation remapping, `flag_injection`, `RESEARCH_REPORT_SYSTEM_PROMPT` synthesis (8192 `research_report_max_tokens`), and post-draft FACT checks (`validate_citations` + `check_claims` + `citation_coverage`) emitting `verification`.
- **Council Debiasing (`council.py`)**:
  - `_run_anonymous_cross_review` runs up to 2 concurrent reviewers over shuffled candidate orders, expands source context slice to `8000` chars, and excludes self-review scores when non-self reviewers exist.
- **Job Lifecycle, Resumable SSE & UI (`research_jobs.py`, `api/v1/research.py`, `ResearchPanel.tsx`)**:
  - Added `GET /v1/research` (owner-scoped DB + memory history), `POST /v1/research/{research_id}/cancel`, `after_seq`/`Last-Event-ID` SSE replay with `id:` lines, full metadata/citation-title restoration from `Query.params`, and corresponding Web UI controls (History selector, Cancel button, Tool/Outline badges, FACT Verification strip, Conflicts accordion, `.md` export).

---

## 4. Wave 4 — Evaluation Suite & Quality Gates

- Fixed `ndcg_at_k` duplicate substring matching (`<= 1.0` invariant) and added `numeric_fidelity_rate`, `citation_precision_rate`, `latency_percentiles`, and `aggregate_by_slice` in `eval_metrics.py`.
- Cleaned `evals/golden_v3_vi.json` and upgraded `scripts/eval_v2.py` (`--judge`, snippet-grounded `claim_support`, latency percentiles, slice breakdown).
- Added offline benchmarks & evaluators:
  - `evals/sentiment_golden.json` (64 bilingual cases) + `app/eval_sentiment.py` + `scripts/eval_sentiment.py` (Macro-F1 >= 0.85 gate).
  - `evals/search_offline_ir.json` (offline IR retrieval & rerank diversity benchmark).
  - `evals/research_golden.json` + `app/eval_research.py` + `scripts/eval_research.py` (FACT report evaluator).

---

## 5. Wave 5 — Answer Quality & Market Intelligence Orchestration Layer (docs/30 BP follow-through, 2026-11-19)

| Area | Change | Verification |
|---|---|---|
| BP-1 Thread carry | `AnswerRequest.thread_id` → owner-checked prior-answer load (`_load_thread_context`, fail-open on invalid id) → one cheap LLM hop rewrites the follow-up as a standalone keyword query before retrieval; `thread_rewrite` event `{original, rewritten}` is a yielded pipeline event (observable by direct `run()` and the SSE bridge alike); broken rewrite fails open. | `tests/test_v30_thread_and_citation.py::TestLoadThreadContext`, `::TestThreadRewrite` |
| BP-4 Citation streaming | `citation_map` SSE event (`n/url/title/chunk_id`) emitted inside the try-block BEFORE the token stream — the UI renders clickable citation chips immediately; the final validated `citations` event supersedes it. Frontend: `onCitationMap`/`onThreadRewrite` handlers (`frontend/lib/api.ts`), wired in `page.tsx`. | `tests/test_v30_thread_and_citation.py::TestCitationMapSse` + `frontend/lib/api.test.ts` |
| Intel: peer discovery | `retrieval/intel.py::discover_peers` — NAME-IDENTITY overlap (parent-affiliate / brand links): diacritic-folded tokens from `company_name`+`short_name`, IDF-weighted with a df ceiling (`max_df_fraction=0.01`, floor 2) so generic corporate verbs can never bridge industries. **Scope honesty: the registry has no sector field — this surfaces identity links (BID↔BSI/BIC "BIDV", FPT↔FRT/FTS on the real registry), not sector clusters; empty peers beats a fabricated cluster.** | `tests/test_intel.py::TestPeerDiscovery` |
| Intel: alert engine | `evaluate_alert_conditions` — Wilder RSI-14 bands, volume spike (≥2.5× prior-10-day avg), 5-day MA cross, 20-day closing-high proximity (explicit `NEAR_20D_CLOSE_HIGH` — computed on closes) with flat-series guard; close/volume filtered ONCE per bar so the two series never desync; ≥10 bars required else no alerts (fail-open, no fabrication). | `tests/test_intel.py::TestAlertEngine` |
| Intel: event impact | `compute_event_impact` — 1-day + N-day return + volume ratio around a tagged event date; missing/invalid/stale data → `null`/`None`, never fabricated; **forward truncation is null too** (fewer forward bars than `window_days` → window return `None`, `bars_used` keeps the true span); tz-aware event dates convert via `astimezone` (not re-label); dchart 00:00-UTC convention pinned in a comment. | `tests/test_intel.py::TestEventImpact` |
| API surface | `GET /v1/market/peers?symbol=` (registry CPU only) + `POST /v1/market/alerts` (1..5 symbols; VNDIRECT→FA-aggregator fallback under one wall-clock cap; empty alerts + no source when no candles). Rate-limit payload-proportional `2 + min(symbols, 5)`; OpenAPI snapshot regenerated. | `tests/test_market_intel.py` + `tests/test_w5_guardrails.py` |
| Fix | `thread_rewrite` originally emitted via `on_event` only — direct `run()` consumers could not observe it; now yielded. | covered by `TestThreadRewrite` |

## 5.1 Round-2 adversarial audit of Wave 5 (2 read-only auditors, 2026-11-19) — findings + fixes

Auditors: (a) security/architect on BP-1/BP-4 + SSE mapping; (b) QA-perf/quant on intel + market endpoints. Combined 16 findings (0 P0/P1 blocking at triage; 1 P1 real-data precision bug found by live probe of the 1518-ticker registry), all fixed in v4.1.1:

| Finding | Severity | Fix |
|---|---|---|
| Peer discovery presented banks as steel peers on real data (generic token `phát` bridged BID↔HPG; `ngân`/`hàng` were already stop-worded — the old test's premise was false) | P1 | IDF-weighted identity tokens + df ceiling + folding + short_name merge (see §5 scope note); test rebuilt against padded registry emulating real df |
| Thread rewrite ran BEFORE the cache check and unmemoized — every resend of the same threaded turn paid an extra LLM call AND defeated the an:v4 cache (non-deterministic rewrite → new key); rewrite Usage never recorded in metrics | P2 | Cache-check raw follow-up first; rewrite only on miss, memoized by `(prior_answer_id, follow-up)` in a bounded 256-entry LRU (`_BoundedMemo`) so the key is deterministic; `record_usage(effective_model, usage)` for the hop; second cache lookup on the rewritten query |
| Rewrite output (unsanitized LLM text) became the retrieval query + cache key | P3 | Whitespace/control-char collapse + hard 200-char cap (`_REWRITE_MEMO` memoizes the sanitized form) |
| Comment claimed "a failing LLM dispatch doesn't emit a dangling citation_map" — false (map yields before `gateway.stream()`) | P3 | Comment corrected to the actual provisional-map contract (final `citations` event supersedes; JSON mode ignores it) |
| JSON mode silently dropped `thread_rewrite` — response gave no indication the query was substituted | P3 | `AnswerResponse.rewritten_query` (null when no rewrite) captured from the event in `_collect` |
| Anonymous mode is ONE shared tenant (auth-off sets owner literal `"anonymous"`) — thread carry can load another anonymous caller's prior answer; docs oversold "owner-checked" isolation | P2 | Posture kept (consistent with the rest of the anonymous surface; auth-off is loopback-only by startup validation), docs corrected, behavior PINNED by `test_anonymous_mode_is_one_shared_tenant` |
| `_load_thread_context` DB read ran outside admission control | P3 | Resolved lazily INSIDE the admitted region (JSON body / SSE producer) via `thread_loader` |
| `market_alerts` gather without `return_exceptions`/outer cap — one bad symbol could 500 a surface documented to never fail | P2 | Per-symbol isolation + single outer wall-clock cap, mirroring fa/compare |
| `compute_event_impact` mislabeled horizon on forward truncation ("5d return" computed over 2 bars) | P2 | Window return `None` when `bars_used < window_days` |
| `_distinctive_tokens` not diacritic-folded (latent split risk on registry refresh); `NEAR_20D_HIGH` computed on closes but named as intraday high; closes/volumes compacted independently (desync risk) | P2/P3 | Folding wired in; condition renamed `NEAR_20D_CLOSE_HIGH`; single per-bar filter |

Verified-correct during the same audit (no change needed): RSI-14 Wilder recursion + edges, MA5 cross windows, volume-spike off-by-one, event-impact staleness guard, `run_cpu` kwargs forwarding, `_fetch_alert_candles` cancellation semantics (outer cancel propagates, own timeout → None), rate-limit body re-read safety (starlette caches `request._body`), citation_map chunk provenance (in-memory request fan-out only; space chunks owner-filtered), no `None==None` cross-tenant leak on the HTTP surface (owner literal `"anonymous"`).

Deferred (docs/30 backlog): BP-2 query decomposition (sub-question fan-out), alert *rules* persistence + push delivery (current surface is poll-on-demand evaluation), portfolio P&L positions model, event-impact batch endpoint over the wire/events feed, thread carry UI (send `thread_id` from follow-up chips), per-session anonymous identity (replaces the shared `"anonymous"` bucket), sector metadata source for true sector peers.
