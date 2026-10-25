# 03 — Search Pipeline (chi tiết 11 stage)

## 1. Bảng stage + latency budget

| # | Stage | Budget | Chi tiết |
|---|---|---|---|
| 1 | Auth + rate limit | 5ms | API key lookup (Redis), sliding window |
| 2 | Normalize + lang detect | 10ms | Unicode NFC, trim, fastText lang id |
| 3 | Cache check (L1) | 2ms | Key = sha1(normalized_query + params); hit → trả ngay |
| 4 | Intent classify | 150–300ms | LLM nhỏ hoặc classifier; cache theo query fingerprint |
| 5 | Fan-out search | 800–2000ms | 1 call SearXNG (đã gom 70+ engines) + vertical adapters theo intent; timeout per engine; partial OK |
| 6 | Merge + dedup + score | 50ms | Canonical URL, SimHash, consensus, diversity |
| 7 | Select top-K | ~0ms | K=10 (quick), K=15 (research) |
| 8 | Scrape song song | 1–4s | Semaphore 4–6 (16GB), timeout 8s/URL, ≤2 URL/domain, SSRF guard |
| 9 | Extract → chunk | 100ms | Markdown sạch; chunk 512 tokens, overlap 64, giữ heading path |
| 10 | Rerank | 0ms (heuristic) | Heuristic-first (consensus+keyword+freshness); tuỳ chọn int8 ONNX ~300MB |
| 11 | Synthesize + persist | TTFT ~1s | LLM streaming; persist/cache async sau khi stream xong |

**Tổng quick answer: p50 4–6s, p95 <10s.** Deep research: 30–180s (nhiều vòng).

## 2. Stage 5 — Fan-out chi tiết

### Engine Registry (mượn từ multi-search-aggregator)

```python
@dataclass
class EngineConfig:
    id: str                 # "searxng", "tavily", "arxiv"
    name: str
    category: str           # general | academic | developer | social | news
    adapter: str            # import path của adapter class
    weight: float = 1.0     # authority weight trong consensus scoring
    enabled: bool = True
    timeout_ms: int = 3000
    max_results: int = 15
    requires_key: bool = False
```

- Lưu trong Postgres `engines` + cache Redis; API `PATCH /v1/engines/{id}` đổi `enabled/weight/timeout` **không restart** (hot config).
- Engine thiếu API key → tự skip, không lỗi (graceful degradation).
- Health tracker: 3 lỗi liên tiếp → auto-disable + alert; recovery probe mỗi 5 phút.

### Adapter interface

```python
class SearchAdapter(Protocol):
    async def search(self, query: str, *, lang: str, max_results: int,
                     timeout_s: float) -> list[SearchResult]: ...
```

- **SearXNGAdapter** (primary): 1 call `GET /search?q=&format=json&language=vi` → đã gom Google/Bing/DDG/Brave…; bật `safesearch`, `categories=general,news`.
- **TavilyAdapter / SerperAdapter / BraveAdapter**: API key từ env; chỉ bật khi có key.
- **Vertical adapters**: arXiv (API), Stack Overflow (API), Reddit (JSON), Wikipedia (API) — bật theo intent (`code` → SO, `academic` → arXiv).

### Chiến lược timeout & partial

- Mỗi engine có timeout riêng (SearXNG 3s, vertical 2s).
- `asyncio.wait(..., timeout=TOTAL_BUDGET)` → lấy kết quả engine nào xong; **không bao giờ fail cả request vì 1 engine chậm**.
- Ghi `engine_health` (success/latency) async.

## 3. Stage 6 — Merge & Scoring

### Canonicalize URL
```
strip utm_*, fbclid, gclid, ref → bỏ fragment → bỏ www. → lowercase host → sort query params
```

### Điểm tổng hợp (multi-signal, mượn từ multi-search-aggregator + mở rộng)

```
score = 0.35 × keyword_relevance      # BM25-lite trên title+snippet
      + 0.25 × consensus              # log2(1 + số engine cùng trả) / log2(1 + max_engines)
      + 0.15 × engine_weight          # weight trung bình có trọng số của các engine trả về
      + 0.10 × freshness              # exp(-age_days / 30) cho intent news; 0.05 cho khác
      + 0.10 × domain_authority       # bảng whitelist/trust score tùy chọn
      + 0.05 × snippet_quality        # độ dài snippet hợp lý, không rác
```

### Near-duplicate
- Exact: canonical URL trùng → giữ bản score cao nhất, gộp `found_by` list.
- Near: SimHash 64-bit trên title+snippet, Hamming distance ≤ 3 → gộp.

### Diversity
- Cap 2–3 kết quả/domain sau khi sort; vòng lặp chọn: lấy top1, đếm domain, nếu vượt cap thì đẩy xuống cuối pool.

## 4. Stage 8–9 — Scrape & Extract

### SSRF guard (bắt buộc, xem [10-security](10-security-compliance.md))
1. Chỉ `http`/`https`; block user/pass trong URL.
2. Resolve DNS → check IP không thuộc private/loopback/link-local/metadata (169.254.169.254).
3. Pin IP đã check khi connect (chống DNS rebinding).
4. Max 3 redirects (re-check mỗi redirect), max 5MB, timeout 8s.
5. robots.txt check + UA `MetaSearchBot/1.0 (+https://metasearch.example/bot)`.

### Fallback chain
```
trafilatura (nhanh, ~50–200ms, phủ 80% trang tĩnh)
  → crawl4ai (Playwright/Chromium + readability + markdown, ~0.5–2s, phủ thêm 15%)
  → jina reader r.jina.ai (fallback cuối, cần internet, ~1s)
```
- Negative cache: URL lỗi 4xx/5xx/timeout → cache 1h, không retry trong request.
- Chunking: 512 tokens, overlap 64, metadata `{url, title, heading_path, position}`.

## 5. Stage 10 — Rerank (heuristic-first)

- **Mặc định (0ms)**: heuristic score = 0.5×consensus_norm + 0.3×keyword_relevance + 0.2×freshness — sort top-30 chunks → top-10. Không load model nào.
- **Tuỳ chọn 1 (nếu cần)**: `jina-reranker-v2-base-multilingual` int8 ONNX (~300MB RAM, ~100–200ms CPU) — bật bằng env `RERANKER=onnx`.
- **Tuỳ chọn 2**: API rerank (Jina/Cohere) — chất lượng tốt nhất, ~$0.001/query.
- **KHÔNG** chạy `bge-reranker-v2-m3` local trên máy 16GB (~2.3GB fp32, CPU 2–3s) — xem [ADR-008](adr/ADR-008-hardware-inference.md).
- Fallback nếu reranker lỗi: giữ thứ tự heuristic (không fail).

## 6. Stage 11 — Synthesize

- Prompt: system (role + quy tắc citation) + context (chunks đánh số `[n]`) + question.
- Quy tắc bắt buộc: **mọi claim phải có `[n]`**; không biết thì nói không biết; trả lời bằng ngôn ngữ của câu hỏi.
- Streaming SSE: `token` events → client render dần; kết thúc bằng `done` event kèm `citations[]` đã validate.
- Post-validate: regex tách `[n]` → map về chunk thật; citation không hợp lệ → strip + log (eval metric).

## 7. Cache 2 tầng

| Tầng | Nơi | Key | TTL |
|---|---|---|---|
| L1 search results | Redis | `sr:{sha1(query+params)}` | 15–60m (news 15m) |
| L1 scraped page | Redis | `pg:{sha1(url)}` | 6–24h |
| L1 answer | Redis | `an:{sha1(query+model+params)}` | 1–6h |
| L2 semantic | pgvector | cosine(query_embedding, cached) > 0.95 | 7d |

- L2 chỉ áp dụng cho answer (không áp cho raw search vì kết quả web thay đổi).
- L2 cần embedding model: trên máy 16GB dùng e5-small int8 (384 dims, ~150MB) hoặc tắt L2 (mặc định dev); prod dùng bge-m3 1024 dims.
- Cache write async (không tăng latency response).
