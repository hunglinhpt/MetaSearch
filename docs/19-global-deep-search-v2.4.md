# docs/19 — v2.4.0 Global Deep Search: mở toàn cầu + tiếng Anh + deep mode

> Vấn đề: tìm kiếm hiện chủ yếu là thị trường VN / tiếng Việt. Mục tiêu v2.4.0:
> tìm **mọi thông tin theo yêu cầu sâu nhất có thể** trên toàn cầu, tiếng Anh là
> ngôn ngữ chính (giữ VI), bao gồm diễn đàn, mạng xã hội, GitHub, Reddit, báo/tạp
> chí học thuật, báo cáo quỹ (fund filings 13F, fact sheet, macro institutions).

## 1. Hiện trạng (đầu vào)

- 19 engines: general (searxng/ddg/tavily/serper/brave) + vertical: arxiv, wikipedia,
  stackoverflow, reddit, github, pubmed, huggingface, semanticscholar, yahoo_finance,
  sec_edgar, stooq, google_news, viet_stock, fengshui.
- SearXNG chỉ enable ~11 engine general (xem `deploy/searxng/settings.yml`) — **đây
  là đòn bẩy lớn nhất**, gần như miễn phí: searxng đã có sẵn engine cho reddit,
  stackexchange, github, youtube, mastodon, x/twitter, linkedin, arxiv, pubmed,
  crossref, dblp, wikidata, semantic scholar…
- `lang` đã có ở schema (`SearchRequest.lang`) và được plumbed tới searxng (language),
  ddg (region), wikipedia (subdomain); **chưa** tới google_news (hl/gl), và chưa có
  ở các adapter mới.
- Category routing: `infer_categories_from_query` (pipeline.py) — thiếu các từ khóa
  social (reddit, hacker news, bluesky, mastodon, x.com…), academic mở rộng (doi,
  ssrn, journal, preprint…), global-finance (etf, 13f, prospectus, imf, world bank…).
- `deep` chưa tồn tại: fan-out luôn bị lọc theo category; muốn "sâu nhất" phải bảo
  server dùng TẤT CẢ engine.

## 2. Plan các wave

| Wave | Nội dung | File chính |
|---|---|---|
| W1 | 7 adapter keyless mới: `hackernews` (HN Algolia), `stackexchange` (api.stackexchange.com), `bluesky` (public.api.bsky.app searchPosts), `openalex`, `crossref`, `europepmc` (academic journals), `gdelt` (global news/events, hỗ trợ `sourcelang` + timespan) | `api/app/retrieval/adapters/{hackernews,stackexchange,bluesky,openalex,crossref,europepmc,gdelt}.py` + registry + engine_store seeds |
| W2 | SearXNG: enable thêm engines (social/forums/code/academic) trong `deploy/searxng/settings.yml`, `default_lang: auto`; adapter map category MetaSearch → category SearXNG (news→news, social→social media, developer→it, academic→science) để không gửi category searxng không biết | `deploy/searxng/settings.yml`, `adapters/searxng.py` |
| W3 | Lang end-to-end: google_news map lang→hl/gl/ceid (en→en/US, vi→vi/VN); gdelt thêm `sourcelang:`; openalex filter `language:`; UI chọn ngôn ngữ Auto/VI/EN | `adapters/google_news.py`, `adapters/gdelt.py`, `adapters/openalex.py`, frontend |
| W4 | `deep: true` trên `/v1/search` + `/v1/answer`: fan-out TẤT CẢ engine đã enable (bỏ lọc category), k tối đa 50; mở rộng `infer_categories_from_query` thêm social/academic/global-fin terms | `schemas.py`, `pipeline.py` (`SearchParams.deep`, `_fanout`) |
| W5 | Frontend: language selector + checkbox "Deep search" + chip category đã có sẵn (social/academic/developer) | `frontend/app/page.tsx`, `lib/api.ts` |
| W6 | Tests + docs + release: `tests/test_global_adapters.py` (MockTransport từng adapter), pipeline deep/inference, searxng category map; OpenAPI snapshot regen; docs/12, README song ngữ, AGENTS.md, CHANGELOG [2.4.0]; `release.py --version 2.4.0 --push` | — |

## 3.Nguồn資料 được cover (mapping yêu cầu → nguồn)

| Yêu cầu của user | Nguồn sau v2.4.0 |
|---|---|
| Diễn đàn | StackExchange API (stackoverflow + toàn bộ site SE), searxng `stackexchange`, Discourse/forum qua general web, HN comments |
| Mạng xã hội | Reddit adapter + searxng reddit; Bluesky public API; Mastodon/X/LinkedIn/YouTube qua searxng (một số có thể bị nền tảng chặn — có breaker, không làm chết request); Telegram/Discord/Facebook không có API public — tìm qua web (ddg/searxng) + kênh public t.me |
| GitHub | GitHub adapter (search API, token tuỳ chọn) + searxng github + searchcode qua searxng |
| Reddit | Reddit adapter (JSON keyless) + searxng |
| Tạp chí học thuật / quỹ | arXiv, PubMed, Europe PMC, Semantic Scholar (đã có) + OpenAlex, Crossref, DBLP qua searxng; Google Scholar qua searxng; SSRN/research institute qua web |
| Báo cáo quỹ & macro toàn cầu | SEC EDGAR (13F/portfolio US funds), Stooq, Yahoo Finance (đã có) + GDELT global events + tin tức thế giới Google News (gl/hl theo lang) |
| "Sâu nhất có thể" | `deep=true` fan-out mọi engine + k=50 + rerank; kết hợp tab Deep Research (FR-9 multi-hop) cho câu hỏi cần tổng hợp |

## 4. Quy ước bắt buộc (giữ nguyên)

- Không key mới bắt buộc: mọi adapter mới đều keyless (polite-pool mailto tuỳ chọn
  qua `GLOBAL_SEARCH_MAILTO` cho OpenAlex/Crossref). Có key thì thêm tuỳ chọn như
  pattern hiện tại (github_token…).
- Graceful degradation: engine mới lỗi → breaker riêng, không ảnh hưởng engine khác.
- Không ngụy tạo: adapter trả đúng `published_at` từ payload (GDELT `seendate`, HN
  `created_at`, Crossref `issued`, OpenAlex `publication_date`, Europe PMC `firstPublicationDate`),
  thiếu thì None.
- SSRF: mọi URL đều hằng số host + query đã encode; không fetch URL do user nhập ở adapter.
- Error model: `AdapterError` với `Outcome` đúng kind (TIMEOUT/RATE_LIMITED/BLOCKED/UPSTREAM_ERROR).
- Rate limit nội bộ: dùng pool `search` chung, timeout theo EngineConfig, breaker qua health registry.

## 5. Rủi ro & giảm thiểu

| Rủi ro | Giảm thiểu |
|---|---|
| SearXNG engine mới chết (x/twitter, linkedin hay bị block) | searxng tự bỏ qua engine lỗi; breaker ở MetaSearch; không enable engine cần key/searxng-secret |
| GDELT query syntax đặc biệt (`sourcelang:`) | chỉ append khi lang hợp lệ; sai cú pháp → gdelt trả lỗi → adapter trả [] + log, không 500 |
| Bluesky public API đổi schema | parse phòng thủ (dict.get chuỗi), test pin schema giả |
| Fan-out deep quá tải | deep vẫn qua admission + budget + health breaker; k cap 50; timeout tổng có sẵn |
| Ngôn ngữ: query VI nhưng lang=en | UI có Auto (không gửi lang); searxng default_lang=auto; answer prompt theo lang của request (FR-16) |

## 6. Đề xuất "tốt hơn" (đã áp dụng trong plan)

1. Ưu tiên **bật engine có sẵn của SearXNG** trước khi viết adapter mới (giants-first,
   ADR-007) — adapter mới chỉ cho nguồn searxng không có/không tốt (HN, Bluesky,
   StackExchange API, OpenAlex, Crossref, Europe PMC, GDELT).
2. `deep=true` ở mức search (fan-out toàn bộ) thay vì chỉ nới k — vì k lớn trên 1
   category vẫn hẹp; deep mới thực sự "mọi thông tin".
3. Map category MetaSearch→SearXNG thay vì gửi thẳng (tránh searxng nhận category
   không tồn tại như `financial` → có thể 400 cả request).
4. `default_lang: auto` của searxng để nó tự nhận ngôn ngữ query khi client không
   chỉ định — đúng với bài toán toàn cầu song ngữ.

## 7. Checklist

- [x] W1 adapters + seeds + registry (7 engine mới, category đúng)
- [x] W2 searxng settings + category map + default_lang auto
- [x] W3 lang plumbing (google_news/gdelt/openalex) + UI selector
- [x] W4 deep flag end-to-end + inference terms mới
- [x] W5 frontend controls
- [x] W6 tests (MockTransport từng adapter, deep, inference, searxng map), OpenAPI regen
- [x] Gates: ruff/format/bandit/mypy/pytest/vitest/tsc/build xanh
- [x] Audit (child-limit của agent runner chạm trần nên tự audit 8 lens fresh-eyes 2 vòng: fix P1 upgrade-path seed, kiểm tra SSRF/lang-injection/cache/deep — hết P0/P1/P2) đối kháng 8 lens × 2 vòng, hết P0/P1/P2
- [x] Docs: README song ngữ, AGENTS.md, docs/12, CHANGELOG [2.4.0], env.example
- [x] Release: v2.4.0 (tag + push + GitHub Release + CI xanh); các fix sau audit → patch v2.4.1

## 8. Audit 3 vòng sau release (2026-11-12, tự audit 8 lens fresh-eyes vì agent runner chạm child-limit)

| Vòng | Phát hiện | Fix |
|---|---|---|
| 1 | 6/7 adapter gọi `payload.get(...)` không guard — upstream trả JSON array/string hoặc item không phải dict sẽ ném AttributeError (breaker ghi nhận lỗi thay vì trả rỗng) | `isinstance` guard ở payload lẫn từng item của cả 7 adapter + 12 test schema-drift mới; `import re` trong hàm của stackexchange dời lên đầu |
| 2 | (a) `infer_categories_from_query` so khớp substring nên "nav" khớp "navigation", "eps" khớp "steps" → category sai; (b) `deep=true` vẫn bị quorum soft-budget cắt engine → không thực sự "sâu nhất" | (a) matcher word-boundary cho term ASCII (lookaround chỉ khi term bắt đầu/kết bằng word char — "doi:" vẫn khớp, cụm Việt giữ substring); (b) deep mode tắt quorum early-return, vẫn bounded bởi hard budget; test 2 chiều shallow/deep |
| 3 | (a) google_news map `lang=en` sai thành `gl=EN` (phải là country code); (b) UI deep mode không nới `k` nên kết quả merge vẫn hẹp | (a) map ngôn ngữ→quốc gia đúng (en→US, vi→VN, fr→FR…, fallback US) + test; (b) UI gửi `k=30` khi bật Deep (schema cap 50), plumbed vào cả search và streamAnswer |

Kết quả sau 3 vòng: full pytest EXIT=0, ruff/format/bandit/mypy (CI scope) sạch, vitest 49/49, tsc + next build xanh. Không còn P0/P1/P2.: tag v2.4.0 + push + CI xanh + GitHub Release
