# 03 — Search Pipeline (chi tiết 11 stage)

## 1. Bảng stage + latency budget

| # | Stage | Budget | Chi tiết |
|---|---|---|---|
| 1 | Auth + rate limit | 5ms | API key lookup (Redis + bounded in-memory cache), RBAC, sliding window |
| 2 | Normalize + lang detect | 10ms | Unicode NFC, trim, heuristic/fastText lang id |
| 3 | Cache check (L1) | 2ms | Key = sha1(normalized_query + params); hit → trả ngay |
| 4 | Smart Category Routing | ~1ms | `infer_categories_from_query` tự động kích hoạt vertical categories khi `categories == ["general"]` |
| 5 | Fan-out search | 800–2000ms | 1 call SearXNG (gom 70+ engines) + 14 vertical adapters theo category; timeout per engine; partial OK |
| 6 | Merge + dedup + score | 50ms | Canonical URL, SimHash 64-bit, consensus, curated domain authority, diversity cap |
| 7 | Select top-K | ~0ms | K=10 (quick), K=15 (research) |
| 8 | Scrape song song | 1–4s | Semaphore 4–6 (16GB), timeout 8s/URL, ≤2 URL/domain, SSRF IP-pinning + redirect re-check |
| 9 | Extract → chunk | 100ms | Markdown sạch; chunk 512 tokens, overlap 64, giữ heading path; bảo toàn dữ liệu định lượng |
| 10 | Rerank | 0ms (heuristic) | Heuristic-first (consensus+keyword+freshness); tuỳ chọn int8 ONNX ~300MB hoặc API rerank |
| 11 | Synthesize + persist | TTFT ~1s | LLM streaming; persist/cache async sau khi stream xong |

**Tổng quick answer: p50 4–6s, p95 <10s.** Deep research: 30–180s (nhiều vòng, sub-queries chạy song song qua `asyncio.gather`).

## 2. Stage 5 — Fan-out chi tiết

### Engine Registry

```python
@dataclass(slots=True)
class EngineConfig:
    id: str                 # "searxng", "viet_stock", "stooq", "arxiv", ...
    name: str
    category: str           # general | academic | developer | social | news | reference | financial | fengshui
    adapter: str            # import path của adapter class
    weight: float = 1.0     # authority weight trong consensus scoring
    enabled: bool = True
    timeout_ms: int = 3000
    max_results: int = 15
    requires_key: bool = False
```

- Lưu trong Postgres/SQLite `engines` + in-memory fallback; API `PATCH /v1/engines/{id}` (RBAC `admin`) đổi `enabled/weight/timeout` **không restart** (hot config).
- Engine thiếu API key → tự skip, không lỗi (graceful degradation).
- Health tracker: 3 lỗi liên tiếp → auto-disable (`consecutive_failures` đồng bộ DB + memory); recovery probe mỗi 5 phút.

### Adapter interface & 19 Search Engines

```python
class SearchAdapter(Protocol):
    async def search(self, query: str, *, lang: str | None = None, max_results: int = 15,
                     timeout_s: float = 3.0, categories: list[str] | None = None,
                     freshness_days: int | None = None) -> list[RawResult]: ...
```

- **SearXNGAdapter** (primary): 1 call `GET /search?q=&format=json&language=vi` → đã gom Google/Bing/DDG/Brave…
- **DuckDuckGoAdapter**: keyless general web search fallback cho Lite profile (không cần Docker).
- **TavilyAdapter / SerperAdapter / BraveAdapter**: API key từ env; chỉ bật khi có key.
- **14 Vertical adapters**:
  - **Tài chính & Pháp lý (`category="financial"`)**:
    - `viet_stock`: Dữ liệu chuyên sâu 14 doanh nghiệp đầu ngành HSX/HNX, Pháp y BCTC theo ngành (`CAMELS` Ngân hàng, `Margin/FVTPL` Chứng khoán, `Người mua trả tiền trước` BĐS/KCN, `Altman Z-Score`, `Beneish M-Score`, `Piotroski F-Score`) & Kỹ thuật định lượng (`Fibonacci Golden Pocket & Extensions`, `Ichimoku Cloud`, `True EMA MACD`, `Wilder's RSI & ATR`, `Stochastic`, `EMA Ribbon`, `Kênh hồi quy tuyến tính`, `8 mẫu hình giá/nến`).
    - `stooq`: Chỉ số vĩ mô toàn cầu (`S&P 500`, `Nasdaq`, `DXY`), Hàng hóa (`Vàng XAU/USD`, `Dầu Brent`), Lợi suất trái phiếu Mỹ (`US 10Y`), Tỷ giá (`USD/VND`), Cổ phiếu quốc tế kèm phân tích kỹ thuật OHLCV.
    - `yahoo_finance`: Báo giá thời gian thực, biên độ 52 tuần, vốn hóa, khối lượng và chỉ báo kỹ thuật (`Wilder's RSI`, `Fibonacci`, `Ichimoku`).
    - `sec_edgar`: Hồ sơ kiểm toán Ủy ban Chứng khoán Hoa Kỳ (`10-K`, `10-Q`, `8-K`, `S-1`).
  - **Phong thủy & Huyền học (`category="fengshui"`)**: `fengshui` (Bát Trạch Cung Phi vạn niên `_digital_root` cho mọi năm sinh `1900..2199`, 60 Hoa Giáp Can Chi, Ngũ Hành nạp âm, Tam Hợp/Tứ Hành Xung).
  - **Tin tức (`category="news"`)**: `google_news` (RSS thời gian thực, tự động nhận diện tiếng Việt `hl=vi&gl=VN`).
  - **Học thuật & Y sinh (`category="academic"`)**: `arxiv` (Atom XML qua `defusedxml`), `pubmed` (NCBI E-utilities), `semanticscholar`.
  - **Lập trình & AI (`category="developer"`)**: `github`, `huggingface`, `stackoverflow`.
  - **Xã hội & Tri thức (`category="social"` / `"reference"`)**: `reddit`, `wikipedia` (kèm `_LANG_RE` chống SSRF).
- **Smart Category Routing (`infer_categories_from_query`)**: khi request chỉ định `categories=["general"]`, hệ thống tự động suy luận các chuyên ngành từ tín hiệu câu hỏi để kích hoạt đúng vertical adapters.

### Chiến lược timeout & partial

- Mỗi engine có timeout riêng (SearXNG 3s, vertical 2–4s).
- `asyncio.wait(..., timeout=TOTAL_BUDGET)` → lấy kết quả engine nào xong; **không bao giờ fail cả request vì 1 engine chậm**.
- Ghi `engine_health` (success/latency) async bounded (`ENGINE_STORE_DB_TIMEOUT_S=0.5`).

## 3. Stage 6 — Merge & Scoring

### Canonicalize URL
```
strip utm_*, fbclid, gclid, ref → bỏ fragment → bỏ www. → lowercase host → sort query params
```

### Điểm tổng hợp (multi-signal)

```
score = 0.35 × keyword_relevance      # BM25-lite trên title+snippet
      + 0.25 × consensus              # log2(1 + số engine cùng trả) / log2(1 + max_engines)
      + 0.15 × engine_weight          # weight trung bình có trọng số của các engine trả về
      + 0.10 × freshness              # exp(-age_days / 30) cho intent news; 0.05 cho khác
      + 0.10 × domain_authority       # bảng _TRUSTED_DOMAINS (sec.gov, hsx.vn, vietstock.vn, stooq.com, arxiv.org...)
      + 0.05 × snippet_quality        # độ dài hợp lý (60–320 chars = 1.0; báo cáo định lượng bảng/metrics = 0.95)
```

### Near-duplicate
- Exact: canonical URL trùng → giữ bản score cao nhất, gộp `found_by` list.
- Near: SimHash 64-bit trên title+snippet, Hamming distance ≤ 3 → gộp.

### Diversity
- Cap 2 kết quả/domain mặc định sau khi sort; riêng các bộ máy định lượng đa chiều (`viet_stock`, `stooq`, `fengshui`, `sec_edgar`) được phép giữ tối đa **4 kết quả/domain** để bảo toàn đủ các lát cắt phân tích (Tổng quan, Pháp y BCTC, Kỹ thuật Fibonacci/Ichimoku, Cổ tức & Giao dịch lớn).

## 4. Stage 8–9 — Scrape & Extract

### SSRF guard (bắt buộc, xem [10-security](10-security-compliance.md))
1. Chỉ `http`/`https`; block user/pass trong URL.
2. Resolve DNS → check IP không thuộc private/loopback/link-local/metadata (169.254.169.254).
3. Pin IP đã check khi connect (`Connection: close`, giữ `Host` header + TLS `sni_hostname`).
4. Max 3 redirects (re-check mỗi redirect + re-check `target_url` trước khi leo thang sang `crawl4ai`/`jina`), max 5MB, timeout 8s.
5. robots.txt check + realistic browser headers + challenge page detection (`is_challenge_page`).

### Fallback chain
```
trafilatura (nhanh, ~50–200ms, phủ 80% trang tĩnh)
  → crawl4ai (Playwright/Chromium + readability + markdown, ~0.5–2s, phủ thêm 15%)
  → jina reader r.jina.ai (fallback cuối, keyless hoặc API key, ~1s)
```
- Negative cache: URL lỗi 4xx/5xx/timeout → cache 1h, không retry trong request.
- Chunking: 512 tokens, overlap 64, metadata `{url, title, heading_path, position}`; bảo toàn `r.snippet` cho các adapter định lượng khi trang web chỉ có tóm tắt ngắn.

## 5. Stage 10 — Rerank (heuristic-first)

- **Mặc định (0ms)**: heuristic score = `0.7 × source_score × position_decay + 0.3 × keyword_overlap` — sort chunks → chọn theo token budget. Không load model nào.
- **Tuỳ chọn 1**: `jina-reranker-v2-base-multilingual` int8 ONNX (~300MB RAM) — bật bằng `RERANKER=onnx`.
- **Tuỳ chọn 2**: API rerank (Jina) — bật bằng `RERANKER=api`.
- Fallback nếu reranker lỗi: giữ thứ tự heuristic (không fail).

## 6. Stage 11 — Synthesize

- Prompt: system (role + quy tắc citation) + context (chunks đánh số `[n]`) + question.
- Quy tắc bắt buộc: **mọi claim phải có `[n]`**; không biết thì nói không biết; trả lời bằng ngôn ngữ của câu hỏi.
- Streaming SSE: `token` events → client render dần; kết thúc bằng `citations` và `done` event.
- Post-validate: regex tách `[n]` → map về chunk thật; citation không hợp lệ → strip.

## 7. Cache 2 tầng

| Tầng | Nơi | Key | TTL |
|---|---|---|---|
| L1 search results | Redis (fallback in-process TTL cache) | `sr:{sha1(query+params)}` | 15–60m (news 15m) |
| L1 scraped page | Redis (fallback in-process TTL cache) | `pg:{sha1(url)}` | 6–24h |
| L1 answer | Redis (fallback in-process TTL cache) | `an:{sha1(query+model+params)}` | 1–6h |
| L2 semantic | pgvector (tự động skip trên SQLite) | cosine(query_embedding, cached) > 0.92 | 7d |
