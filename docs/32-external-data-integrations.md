# docs/32 — External Data Integrations (Free-Key / Keyless) — 2026-11-21

Mục tiêu: bổ sung dữ liệu từ các API bên ngoài theo yêu cầu "Giai Đoạn Tích Hợp
API Free Key" — **mọi tính năng hoạt động KHÔNG cần đăng ký** (keyless-first);
key free của nhà cung cấp là nâng cấp tùy chọn, không bắt buộc.

## 1. Luật thiết kế (bắt buộc)

1. **Keyless-first**: mỗi khả năng phải có ít nhất một nguồn public keyless
   đã probe thật từ egress sản xuất. Không nguồn keyless → endpoint báo 503 kèm
   hướng dẫn cấu hình key (không bao giờ dùng "demo key" của nhà cung cấp —
   demo key trả dữ liệu mẫu = ngụy tạo, vi phạm luật không-bịa-dữ-liệu).
2. **Zero fabrication**: thiếu key / nguồn chết / thiếu field → `null` +
   provenance (`source_name`, `source_url`, `as_of`). Mỗi khối dữ liệu mang
   nguồn riêng, không trộn thành một "giá thật".
3. **Free-tier defense**: TTL cache có hạn (bounded) + single-flight trong từng
   fetcher — burst request không thể đốt quota upstream.
4. **Shared HTTP pool**: mọi fetch qua `get_http_client("search")`, cap 5MB,
   browser headers dùng chung (không dựng client tại chỗ).
5. **W5 Hard Boundaries**: GET-only cho nhóm này (không fan-out POST), mọi
   Query param có cận (`min_length`/`max_length`/`ge`/`le`), schema fields có cận.

## 2. Bản đồ nguồn (đã probe thật 2026-11-21)

| Khả năng | Nguồn keyless (mặc định) | Key nâng cấp (env) | Endpoint |
|---|---|---|---|
| Crypto giá + 24h/vol/mcap | CoinGecko `/simple/price` | `COINGECKO_API_KEY` (demo header trên public host; `CG-*` → pro header) | `GET /v1/market/crypto` |
| Crypto global (mcap, dominance) | CoinGecko `/global` | như trên | `GET /v1/market/crypto/global` |
| FX rates | Frankfurter (ECB, thắng hòa) + `open.er-api.com` (160+ mã, có VND) | `EXCHANGERATE_API_KEY` (host keyed v6) | `GET /v1/market/fx` |
| Thời tiết (hiện tại + dự báo 7 ngày) | Open-Meteo forecast + geocoding | `OPENWEATHER_API_KEY` (thêm khối current OWM) | `GET /v1/weather` |
| Tin tức quốc tế | — (google_news engine của MetaSearch là đường keyless) | `NEWSAPI_KEY` (NewsAPI; thiếu key → 503 + hint) | `GET /v1/news/external` |
| Quote cổ phiếu toàn cầu | Yahoo Finance chart API | `TWELVEDATA_API_KEY`, `ALPHA_VANTAGE_API_KEY` (mỗi nguồn một khối, không trộn) | `GET /v1/market/global-quote` |
| Báo cáo tài chính (statements) | — | `FMP_API_KEY` (thiếu key → 503 + hint) | `GET /v1/market/fundamentals` |
| IP geolocation | ipwho.is | `IPINFO_TOKEN` (thêm khối IPinfo) | `GET /v1/net/ipgeo` |

Đã loại trừ: Stooq CSV (egress này bị chặn trang HTML — probe 2026-11-21);
served thay thế bằng Yahoo chart keyless.

## 3. Kiến trúc

```
api/app/retrieval/ext_data/
  __init__.py        — exports
  _base.py           — TtlCache (monotonic, bounded) + ext_settings_keys + json_get
  crypto.py          — CoinGecko (keyless; key-tag cache phân biệt keyless/keyed)
  fx.py              — Frankfurter + ER-API merge (ECB thắng hòa) + keyed host
  weather.py         — Open-Meteo geocode+forecast (WMO code → text chuẩn) + OWM
  news_ext.py        — NewsAPI key-gated (everything/top-headlines)
  markets_global.py  — Yahoo keyless + TwelveData/AlphaVantage blocks + FMP
  ipgeo.py           — ipwho.is keyless + IPinfo block
api/app/api/v1/extdata.py — GET router (8 endpoint), auth theo _auth của /v1
```

Cache-key chứa **key-tag** (sha256 8 ký tự đầu của key) — phân biệt biến thể
keyless/keyed mà không lưu secret. Single-flight chống stampede cùng key.

## 4. Kiểm định (đã chạy)

- 22 unit test MockTransport (`tests/test_ext_data.py`): happy-path từng nguồn,
  merge FX (ECB thắng hòa), key gating (503 + hint), bounds/400, dead upstream
  fail-open, WMO code decode, header demo/pro của CoinGecko.
- W5 guardrails + OpenAPI snapshot regen (8 route mới) xanh; mypy 189 file xanh.
- Live smoke keyless (server thật, dữ liệu thật): crypto BTC/ETH, fx USD→VND/EUR,
  weather Hanoi, global-quote AAPL, ipgeo 1.1.1.1 — provenance + as_of đầy đủ.

## 5. Việc còn lại (backlog)

- MCP tools cho crypto/fx/weather (REST đã xong).
- UI panels cho nhóm nguồn mới (tab Tin tức có thể trộn NewsAPI khi có key).
- Open-Meteo air-quality (PM2.5) + OWM One Call 3.0 forecast khi cần.
- Sector metadata cho peer discovery (docs/31 §5.1) — các nguồn trên không cung cấp.

---

## 6. Lite profile upgrades (2026-11-21 — extraction pipeline + global search expansion)

### 6.1 Extraction pipeline cho Lite (trafilatura → crawl4ai → Jina)

Trước: tier crawl4ai trỏ về Docker hostname `http://crawl4ai:11222` — trên Lite
không có service → mỗi 60s thử lại fail (DNS fail + cooldown 60s), tier thực
chết. Sau:

- **Tier 1 trafilatura**: in-process, luôn chạy (không đổi).
- **Tier 2 crawl4ai (optional, autodetect)**: trỏ `CRAWL4AI_URL=http://127.0.0.1:11235`
  khi đã cài server local (theo docs crawl4ai — `pip install crawl4ai` + serve
  FastAPI); connection refused trên **loopback → cooldown 300s fast-fail**
  (`_is_local_crawl4ai`), remote host giữ 60s (lỗi tạm thời). Không còn đốt
  thời gian mỗi request khi tier vắng.
- **Tier 3 Jina Reader**: keyless `r.jina.ai` (~20 rpm) — `JINA_KEYLESS=true`
  mặc định; `JINA_API_KEY` nâng ceiling.
- `.env.lite.example` cập nhật chú thích 3-tier + giá trị 11235.

### 6.2 Global search expansion cho Lite

Live check (2026-11-21): Lite đã có 25/29 engines enabled (tất cả adapters
keyless docs/19 + bing_news/vn_wire/fin_rss docs/20). Điểm nghẽn thật:

1. **Web tổng hợp chỉ có DDG** → thêm `bing_web` (Bing Web Search RSS,
   keyless — probe thật trả kết quả web + tiếng Việt; reuse unwrap helpers
   của bing_news). EngineConfig: category `general`, weight 0.9.
2. **7 adapters Last30Days (docs/26) có trong `build_adapters` nhưng KHÔNG có
   EngineConfig → không bao giờ được fan-out (dead code)**. Bổ sung config cho
   các adapter **query-aware**: `polymarket`, `stocktwits`, `careers`
   (enabled mặc định, keyless graceful); `uspto` + `uspto_company`
   **key-gated** — live probe 2026-11-21 cho thấy endpoint keyless cũ
   (`developer.uspto.gov/ibd-api`) đã retired (301 về trang chủ), host mới
   `api.uspto.gov` yêu cầu token (`USPTO_API_KEY`, miễn phí đăng ký USPTO
   Open Data Portal) → không key thì 2 engines tắt (không đốt call chết).
   Hai adapter **query-blind** (`polymarket_trending`, `stocktwits_trending`)
   cố tình KHÔNG có EngineConfig — bật sẽ pollute relevance bằng kết quả
   không liên quan query; vẫn dùng được qua hot-config khi cần.

Sau thay đổi: Lite fan-out 29 engines enabled (25 cũ + bing_web +
polymarket/stocktwits/careers; uspto* bật khi có key), 35 configs —
searxng/tavily/serper/brave vẫn disabled đúng vai trò. Toàn bộ nhóm mới
keyless trừ uspto (free key tùy chọn).
