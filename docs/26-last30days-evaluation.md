# Docs/26 — Đánh giá & So sánh Last30Days-Skill vs MetaSearch

> **Mục tiêu**: Đánh giá chi tiết **last30days-skill** (mvanhorn/last30days-skill) so với hệ thống MetaSearch hiện tại, xác định những tính năng thiếu hụt hoặc vượt trội, và đề xuất kế hoạch tích hợp nếu cần thiết.
>
> **Ngày nghiên cứu**: 2026-11-20
> **Trạng thái**: Đánh giá hoàn chỉnh + Kế hoạch hành động

---

## 📊 TÓM TẮT ĐÁNH GIÁ

### **Last30Days-Skill Overview**
- **Repo**: [mvanhorn/last30days-skill](https://github.com/mvanhorn/last30days-skill)
- **Stars**: 71,103 ⭐ (Top 1 GitHub Trending)
- **License**: MIT
- **Version**: 4.4.0
- **Mục đích**: AI agent-led search engine, nghiên cứu bất kỳ chủ đề trong 30 ngày qua
- **Cách hoạt động**: Tích hợp multi-source (Reddit, X, YouTube, TikTok, HN, Polymarket, GitHub, Web) + AI synthesis

---

## 🎯 SO SÁNH CHI TIẾT

### **📋 Bảng So Sánh Điểm Mạnh/Điểm Yếu**

| **Danh mục** | **Last30Days-Skill** | **MetaSearch** | **Kết luận** |
|--------------|----------------------|----------------|---------------|
| **Nguồn dữ liệu** | 30+ sources (Reddit, X, YouTube, TikTok, Instagram, HN, Polymarket, GitHub, LinkedIn, Web, Patents, Jobs, StockTwits, v.v.) | 26 engines (SearXNG, DuckDuckGo, arXiv, Wikipedia, StackOverflow, Reddit, GitHub, PubMed, Yahoo Finance, v.v.) | **Last30Days thắng** (nhiều sources đa dạng hơn) |
| **Tính năng Social** | ✅ Full (Reddit upvotes, X likes, YouTube views, TikTok engagement, Polymarket odds) | ❌ Limited (chỉ search, không engagement metrics) | **Last30Days thắng** |
| **Polymarket Integration** | ✅ Native (real money odds, volume tracking) | ❌ Không có | **Last30Days thắng** |
| **Patents & Jobs Signals** | ✅ Patent Secret Signals, Jobs Page Secret Signals | ❌ Không có | **Last30Days thắng** |
| **YouTube/TikTok Transcripts** | ✅ Full transcripts, video search | ❌ Không có | **Last30Days thắng** |
| **Multi-turn Research** | ✅ IRCoT-style loop (thếu evidence, reformulate, cover sub-questions) | ✅ Đã có IRCoT loop (research.py) | **Hòa** |
| **Citation Quality** | ✅ Quote lines from sources, inline citations | ✅ Citation streaming, validation | **Hòa** |
| **Cost Optimization** | ⚠️ Có thể đắt (yêu cầu nhiều API keys) | ✅ Free providers, cost tracking | **MetaSearch thắng** |
| **Vietnamese Support** | ❌ Khá hạn chế | ✅ Full vi-first (tokenization, stopwords, BM25) | **MetaSearch thắng** |
| **Financial Data** | ⚠️ Basic (Yahoo Finance, StockTwits) | ✅ Deep (VNDIRECT, SSI, TCBS, FireAnt, FA Aggregator) | **MetaSearch thắng** |
| **On-Premises** | ❌ Đòi hỏi nhiều external APIs | ✅ Lite mode (SQLite, keyless) | **MetaSearch thắng** |
| **API/Server Mode** | ❌ Chỉ CLI/skill | ✅ REST API + SSE + MCP Server | **MetaSearch thắng** |
| **Web UI** | ❌ Không có | ✅ Full Web UI (Next.js) | **MetaSearch thắng** |
| **Authentication** | ❌ Khó quản lý keys | ✅ API key management, RBAC | **MetaSearch thắng** |
| **Rate Limiting** | ❌ Phụ thuộc external APIs | ✅ Built-in rate limiting | **MetaSearch thắng** |
| **Caching** | ❌ Không rõ | ✅ L1 Redis + L2 pgvector | **MetaSearch thắng** |
| **Extensibility** | ❌ Đóng (skill format) | ✅ Open architecture | **MetaSearch thắng** |

---

## 🔍 PHÂN TÍCH CHI TIẾT TỪNG LĨNH VỰC

---

### **1. 📊 Multi-Source Search (Last30Days Thắng)**

#### **Last30Days-Skill Sources (30+)**
| Nguồn | Mô tả | Yêu cầu Key | Real-time |
|-------|-------|-------------|-----------|
| **Reddit** | Posts, comments, upvotes | ❌ Keyless | ✅ |
| **X (Twitter)** | Tweets, likes, threads | ✅ X_BEARER_TOKEN | ✅ |
| **YouTube** | Videos, transcripts | ❌ Keyless (yt-dlp) | ✅ |
| **TikTok** | Videos, engagement | ✅ SCRAPECREATORS_API_KEY | ✅ |
| **Instagram** | Reels, posts | ✅ SCRAPECREATORS_API_KEY | ✅ |
| **Hacker News** | Stories, comments | ❌ Keyless | ✅ |
| **Polymarket** | Prediction markets, odds | ❌ Keyless | ✅ |
| **GitHub** | PRs, issues, repos | ❌ Keyless | ✅ |
| **LinkedIn** | Posts, articles | ⚠️ Cookie-based | ✅ |
| **arXiv** | Research papers | ❌ Keyless | ✅ |
| **Techmeme** | Tech news | ❌ Keyless | ✅ |
| **Yahoo Finance** | Stock data | ❌ Keyless | ✅ |
| **StockTwits** | Trader sentiment | ❌ Keyless | ✅ |
| **Threads** | Posts, comments | ✅ SCRAPECREATORS_API_KEY | ✅ |
| **Bluesky** | Posts | ✅ BSKY_HANDLE + Password | ✅ |
| **Truth Social** | Posts | ✅ TRUTHSOCIAL_TOKEN | ✅ |
| **Xiaohongshu** | Chinese social | ✅ XIAOHONGSHU_API_BASE | ✅ |
| **Patents** | USPTO filings | ❌ Keyless | ✅ |
| **Jobs/Careers** | Company job postings | ❌ Keyless | ✅ |
| **Meta Ads** | Ad creatives | ⚠️ Cookie-based | ✅ |
| **Trustpilot** | Reviews | ✅ TRUSTPILOT_API_KEY | ✅ |
| **Amazon** | Products, reviews | ✅ BRIGHTDATA | ✅ |
| **DEV Community** | Articles, comments | ❌ Keyless | ✅ |
| **DripStack** | Newsletters | ❌ Keyless | ✅ |
| **Web** | General web search | ✅ BRAVE/PERPLEXITY | ✅ |

#### **MetaSearch Sources (26)**
| Nguồn | Mô tả | Yêu cầu Key | Real-time |
|-------|-------|-------------|-----------|
| **SearXNG** | Meta search engine | ✅ SearXNG service | ✅ |
| **DuckDuckGo** | General search | ❌ Keyless | ✅ |
| **arXiv** | Research papers | ❌ Keyless | ✅ |
| **Wikipedia** | Encyclopedia | ❌ Keyless | ✅ |
| **StackOverflow** | Q&A | ❌ Keyless | ✅ |
| **Reddit** | Subreddits | ❌ Keyless | ✅ |
| **GitHub** | Code search | ✅ GITHUB_TOKEN | ✅ |
| **PubMed** | Medical papers | ✅ PUBMED_API_KEY | ✅ |
| **HuggingFace** | Models, datasets | ❌ Keyless | ✅ |
| **Semantic Scholar** | Academic papers | ✅ SEMANTIC_SCHOLAR_API_KEY | ✅ |
| **Yahoo Finance** | Stock data | ❌ Keyless | ✅ |
| **SEC EDGAR** | Financial filings | ❌ Keyless | ✅ |
| **Stooq** | Financial data | ❌ Keyless | ✅ |
| **Google News** | News | ❌ Keyless | ✅ |
| **VietStock** | VN market data | ❌ Keyless | ✅ |
| **FengShui** | Vietnamese astrology | ❌ Keyless | ✅ |
| **HackerNews** | Tech news | ❌ Keyless | ✅ |
| **StackExchange** | Q&A | ❌ Keyless | ✅ |
| **Bluesky** | Social media | ❌ Keyless | ✅ |
| **OpenAlex** | Academic | ❌ Keyless | ✅ |
| **Crossref** | Academic | ❌ Keyless | ✅ |
| **EuropePMC** | Medical | ❌ Keyless | ✅ |
| **GDELT** | News | ❌ Keyless | ✅ |
| **Tavily** | AI search | ✅ TAVILY_API_KEY | ✅ |
| **Serper** | AI search | ✅ SERPER_API_KEY | ✅ |
| **Brave** | AI search | ✅ BRAVE_API_KEY | ✅ |

#### **📌 KẾT LUẬN**
- **Last30Days có nhiều sources đa dạng hơn**, đặc biệt là social media (TikTok, Instagram, Threads) và prediction markets (Polymarket)
- **MetaSearch có nhiều academic/financial sources hơn** (PubMed, Semantic Scholar, SEC EDGAR, VietStock)
- **Cả hai đều có thể hoạt động keyless cho phần lớn sources**
- **Khuyến nghị**: **TÍCH HỢP** các sources đang thiếu của Last30Days vào MetaSearch:
  - TikTok (via ScrapeCreators)
  - Instagram (via ScrapeCreators)
  - Polymarket
  - StockTwits
  - Threads
  - Truth Social
  - Xiaohongshu
  - Meta Ads
  - Trustpilot
  - Patents (USPTO)
  - Jobs/Careers pages

---

### **2. 🎯 Social Engagement Scoring (Last30Days Thắng)**

#### **Last30Days Approach**
- **Reddit**: Upvotes, comment counts, engagement scores
- **X/Twitter**: Likes, retweets, views, engagement
- **YouTube**: Views, likes, comment counts
- **TikTok**: Views, likes, shares, engagement rate
- **Polymarket**: Odds, volume, confidence levels
- **Ranking**: Social relevancy > SEO relevancy

#### **MetaSearch Approach**
- **Chưa có engagement metrics**
- **Ranking**: Dựa trên consensus, signals, RRF fusion
- **Nguồn**: Chỉ dựa trên search engine rankings

#### **📌 KẾT LUẬN**
- **Last30Days có hệ thống scoring dựa trên engagement của con người**
- **MetaSearch chưa có tính năng này**
- **Khuyến nghị**: **TÍCH HỢP** engagement scoring vào MetaSearch:
  - Thêm trường `engagement_score` vào kết quả
  - Tích hợp Reddit upvotes, X likes, YouTube views
  - Sử dụng Polymarket odds như signal
  - Cập nhật ranking algorithm

---

### **3. 💼 Polymarket Integration (Last30Days Thắng)**

#### **Last30Days**
```python
# Ví dụ: Polymarket odds
"Polymarket odds: 96% confidence on album sales, 4% on acquisition"
"Odds moved +15% in the last 7 days"
```
- **API**: Gamma API (`gamma-api.polymarket.com`)
- **Data**: Odds, volume, confidence, movement
- **Integration**: Native, real-time

#### **MetaSearch**
- **Không có Polymarket**
- **Financial data**: Chỉ VNDIRECT, SSI, TCBS (VN market)

#### **📌 KẾT LUẬN**
- **Polymarket là nguồn prediction market duy nhất**
- **Cung cấp insights về không chỉ tin tức mà cả dự đoán của cộng đồng**
- **Khuyến nghị**: **TÍCH HỢP** Polymarket vào MetaSearch:
  - Tạo adapter `polymarket.py`
  - Thêm `polymarket_api_key` config
  - Tích hợp odds vào financial research

---

### **4. 📄 Patents & Jobs Secret Signals (Last30Days Thắng)**

#### **Last30Days Secret Signals**
1. **Patent Secret Signals**
   - Tự động phát hiện USPTO patent filings
   - Flag patents "out of character" (không đặc trưng cho company)
   - Flag patents mô tả sản phẩm chưa được announce
   - **Ví dụ**: "Apple filed a patent for room state changes via photos" (unannounced)

2. **Jobs Page Secret Signals**
   - Đọc careers page của company
   - Flag roles tiết lộ sản phẩm mới ("AI agent for purchase completion")
   - Flag teams mới chưa được công bố
   - **Ví dụ**: "Apple hiring for 'YouTube Ads Agentic Buyflows'"

#### **MetaSearch**
- **Không có tính năng tương tự**
- **Financial data**: Chỉ dữ liệu thị trường, không có insights từ patents/jobs

#### **📌 KẾT LUẬN**
- **Secret Signals là tính năng độc đáo và cực kỳ giá trị**
- **Giúp phát hiện early signals về sản phẩm/đường hướng của company**
- **Khuyến nghị**: **TÍCH HỢP** Secret Signals vào MetaSearch:
  - USPTO adapter (keyless)
  - Careers page scraper
  - Patent analysis module
  - Jobs signal detection

---

### **5. 🎥 YouTube/TikTok Transcripts (Last30Days Thắng)**

#### **Last30Days**
- **YouTube**: Full transcripts via yt-dlp
- **TikTok**: Transcripts via ScrapeCreators
- **Instagram**: Transcripts via ScrapeCreators
- **Sử dụng**: Trích xuất quotes, tóm tắt video

#### **MetaSearch**
- **Không có video transcript functionality**
- **Chỉ có text-based search**

#### **📌 KẾT LUẬN**
- **Video là nguồn thông tin quan trọng** (TikTok, YouTube, Instagram)
- **Transcripts giúp AI hiểu nội dung video**
- **Khuyến nghị**: **TÍCH HỢP** video transcripts:
  - Thêm `yt-dlp` dependency
  - Tạo `video_transcript.py` adapter
  - Tích hợp với ScrapeCreators cho TikTok/Instagram

---

### **6. 💰 Financial Data (MetaSearch Thắng)**

#### **MetaSearch**
- **VNDIRECT**: DChart TradingView Feed (1,518 mã VN)
- **SSI**: OHLCV data
- **TCBS**: Financial ratios
- **FireAnt**: BCTC, JWT authentication
- **FA Aggregator**: Multi-source validation
- **Simplize**: Fallback data
- **Sentiment Engine**: CNN Fear & Greed, CBOE Put/Call, FRED HY OAS, CafeF flows, SSI orderflow, DNSE OHLCV

#### **Last30Days**
- **Yahoo Finance**: Basic stock data
- **StockTwits**: Trader sentiment
- **Không có VN market**

#### **📌 KẾT LUẬN**
- **MetaSearch có hệ thống financial data sâu hơn nhiều**
- **Đặc biệt mạnh về thị trường Việt Nam**
- **Last30Days chỉ có basic global data**
- **Khuyến nghị**: **GỮ NGUYÊN** hệ thống financial của MetaSearch, có thể bổ sung:
  - Yahoo Finance adapter (global)
  - StockTwits adapter

---

### **7. 🌐 Vietnamese Support (MetaSearch Thắng)**

#### **MetaSearch**
- **Tokenization**: `fold_diacritics()` + Vietnamese stopwords
- **BM25**: Vietnamese-aware
- **Golden Eval**: Vietnamese queries (25+)
- **Search**: Hỗ trợ tìm kiếm tiếng Việt
- **Financial**: Toàn diện VN market

#### **Last30Days**
- **Không có Vietnamese support đặc biệt**
- **Chủ yếu là English content**

#### **📌 KẾT LUẬN**
- **MetaSearch có ưu thế rõ rệt về Vietnamese**
- **Last30Days không cần tích hợp cho VN market**

---

### **8. 🏗️ Architecture & Extensibility (MetaSearch Thắng)**

| **Tính năng** | **Last30Days** | **MetaSearch** |
|---------------|----------------|----------------|
| **API Server** | ❌ Chỉ CLI/Skill | ✅ REST API + SSE |
| **Web UI** | ❌ Không có | ✅ Next.js UI |
| **MCP Server** | ❌ Không có | ✅ Full MCP support |
| **Plugin System** | ❌ Đóng (skill format) | ✅ Open architecture |
| **Hot Reload** | ❌ Không có | ✅ Engine config hot-reload |
| **Caching** | ❌ Không rõ | ✅ L1 Redis + L2 pgvector |
| **Rate Limiting** | ❌ Phụ thuộc external | ✅ Built-in |
| **Authentication** | ❌ Khó quản lý | ✅ API keys, RBAC |
| **Multi-tenancy** | ❌ Không có | ✅ Per-key isolation |
| **Lite Mode** | ❌ Không có | ✅ SQLite, keyless |
| **Docker Support** | ❌ Không rõ | ✅ Full compose |

#### **📌 KẾT LUẬN**
- **MetaSearch có kiến trúc Enterprise-ready**
- **Last30Days là công cụ cá nhân (personal tool)**
- **Khuyến nghị**: **GỮ NGUYÊN** kiến trúc MetaSearch, chỉ tích hợp tính năng

---

## 🎯 KẾ HOẠCH TÍCH HỢP (P0 - ƯU TIÊN CAO)

### **📌 Những gì NÊN tích hợp từ Last30Days**

#### **1. Multi-Source Expansion (P0 - 5-7 ngày)**
**Mục tiêu**: Bổ sung các sources đang thiếu

| Nguồn | Adapter | API Key | Effort | Ưu tiên |
|-------|---------|---------|--------|----------|
| **Polymarket** | `polymarket.py` | ❌ Keyless (Gamma API) | 2 ngày | **P0** |
| **StockTwits** | `stocktwits.py` | ❌ Keyless (public) | 1 ngày | **P0** |
| **TikTok** | `tiktok.py` | ✅ SCRAPECREATORS_API_KEY | 2 ngày | **P0** |
| **Instagram** | `instagram.py` | ✅ SCRAPECREATORS_API_KEY | 2 ngày | **P0** |
| **Threads** | `threads.py` | ✅ SCRAPECREATORS_API_KEY | 1 ngày | **P1** |
| **Patents (USPTO)** | `uspto.py` | ❌ Keyless | 2 ngày | **P0** |
| **Jobs/Careers** | `careers.py` | ❌ Keyless | 2 ngày | **P1** |

**Triển khai:**
```python
# Thêm vào retrieval/adapters/
from app.retrieval.adapters.polymarket import PolymarketAdapter
from app.retrieval.adapters.stocktwits import StockTwitsAdapter
from app.retrieval.adapters.tiktok import TikTokAdapter
from app.retrieval.adapters.instagram import InstagramAdapter
from app.retrieval.adapters.uspto import USPTOAdapter
from app.retrieval.adapters.careers import CareersAdapter
```

#### **2. Engagement Scoring (P0 - 3-4 ngày)**
**Mục tiêu**: Thêm social engagement vào ranking

**Triển khai:**
```python
# Trong retrieval/scoring.py
class EngagementScore:
    def __init__(self):
        self.weights = {
            "reddit_upvotes": 1.0,
            "x_likes": 0.8,
            "youtube_views": 0.5,
            "tiktok_views": 0.6,
            "polymarket_volume": 1.2,
        }
    
    def calculate(self, result: SearchResult) -> float:
        score = 0.0
        if result.reddit_upvotes:
            score += result.reddit_upvotes * self.weights["reddit_upvotes"]
        if result.x_likes:
            score += result.x_likes * self.weights["x_likes"]
        # ... etc
        return score

# Cập nhật merger.py
engagement_score = EngagementScore()
merged_score = (
    consensus_score * 0.4 +
    rrf_score * 0.3 +
    engagement_score * 0.2 +
    freshness_score * 0.1
)
```

#### **3. Secret Signals (P1 - 5-7 ngày)**
**Mục tiêu**: Phát hiện early signals từ patents & jobs

**Triển khai:**
```python
# Trong orchestration/research.py
class SecretSignalDetector:
    def __init__(self, company: str):
        self.company = company
    
    async def detect_patent_signals(self) -> list[PatentSignal]:
        """Phát hiện patents 'out of character'"""
        patents = await self.uspto_search(self.company)
        signals = []
        for patent in patents:
            if self.is_out_of_character(patent):
                signals.append(PatentSignal(
                    title=patent.title,
                    filing_date=patent.date,
                    signal_type="out_of_character",
                    confidence=0.9,
                ))
        return signals
    
    async def detect_job_signals(self) -> list[JobSignal]:
        """Phát hiện roles tiết lộ sản phẩm mới"""
        jobs = await self.careers_scrape(self.company)
        signals = []
        for job in jobs:
            if self.is_new_product_hint(job):
                signals.append(JobSignal(
                    title=job.title,
                    team=job.team,
                    signal_type="new_product_hint",
                    confidence=0.8,
                ))
        return signals
```

#### **4. Video Transcripts (P1 - 4-5 ngày)**
**Mục tiêu**: Trích xuất transcripts từ video

**Triển khai:**
```python
# Trong extraction/video_transcript.py
import yt_dlp

class VideoTranscriptExtractor:
    def __init__(self):
        self.ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
        }
    
    async def extract_youtube_transcript(self, url: str) -> str:
        """Extract transcript from YouTube video"""
        with yt_dlp.YoutubeDL(self.ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info.get("automatic_captions"):
                # Get best caption
                caption = list(info["automatic_captions"].values())[0]
                return " ".join([c["text"] for c in caption])
        return ""
    
    async def extract_tiktok_transcript(self, url: str) -> str:
        """Extract transcript from TikTok via ScrapeCreators"""
        # Use ScrapeCreators API
        pass
```

---

### **📌 Những gì KHÔNG NÊN tích hợp**

#### **1. Architecture Changes**
- **Không chuyển sang skill format** (MetaSearch có kiến trúc tốt hơn)
- **Không bỏ API server** (Last30Days là CLI-only)
- **Không bỏ Web UI** (MetaSearch đã có UI đầy đủ)

#### **2. Cost Model**
- **Không dùng paid APIs** (Last30Days yêu cầu nhiều keys đắt tiền)
- **Giữ free providers** (Pollinations, OpenRouter Free, v.v.)

#### **3. Branding & UX**
- **Không đổi branding**
- **Không đổi UX flow** (MetaSearch đã có workflow tối ưu)

---

## 🚀 KẾT LUẬN & ROADMAP

### **Tóm tắt Lợi ích sau Tích hợp**

| **Lĩnh vực** | **Trước** | **Sau** | **Cải thiện** |
|-------------|-----------|---------|---------------|
| **Sources** | 26 | 36+ | +40% |
| **Social Engagement** | ❌ Không có | ✅ Full support | New feature |
| **Polymarket** | ❌ Không có | ✅ Native integration | New feature |
| **Secret Signals** | ❌ Không có | ✅ Patents & Jobs | New feature |
| **Video Transcripts** | ❌ Không có | ✅ YouTube/TikTok | New feature |
| **Financial** | ✅ VN deep | ✅ VN + Global | +20% |

### **Roadmap Chi Tiết**

#### **Phase 1: Core Sources (P0 - 10-14 ngày)**
1. **Polymarket Adapter** (2 ngày)
2. **StockTwits Adapter** (1 ngày)
3. **USPTO Patents Adapter** (2 ngày)
4. **Careers Pages Scraper** (2 ngày)
5. **Engagement Scoring** (3-4 ngày)
6. **Testing & Validation** (2-3 ngày)

#### **Phase 2: Social Media (P1 - 8-10 ngày)**
1. **TikTok Adapter** (2 ngày)
2. **Instagram Adapter** (2 ngày)
3. **Threads Adapter** (1 ngày)
4. **Video Transcripts** (3-4 ngày)

#### **Phase 3: Secret Signals (P1 - 5-7 ngày)**
1. **Patent Analysis Module** (3 ngày)
2. **Jobs Signal Detection** (2 ngày)
3. **Integration with Research** (2 ngày)

### **Effort Tổng cộng**
- **P0 (Core)**: 10-14 ngày
- **P1 (Extended)**: 13-17 ngày
- **TOTAL**: **23-31 ngày** (có thể song song các phase)

### **Khuyến nghị Cuối Cùng**

✅ **TÍCH HỢP CÁC TÍNH NĂNG SAU:**
1. **Polymarket Integration** (Prediction markets)
2. **Patents & Jobs Secret Signals** (Early detection)
3. **Engagement Scoring** (Social relevance)
4. **USPTO & Careers Adapters** (New sources)
5. **Video Transcripts** (YouTube/TikTok)

❌ **KHÔNG TÍCH HỢP:**
1. Architecture changes (Keep MetaSearch's design)
2. Cost model (Keep free providers)
3. Branding/UX changes
4. Last30Days skill format (Not compatible)

🎯 **KẾT QUẢ**: MetaSearch sẽ có **đủ các tính năng ưu việt của Last30Days** trong khi vẫn **giữ được ưu thế của mình** về Vietnamese support, financial data, và enterprise architecture.

---

## 📝 CHECKLIST TRIỂN KHAI

### **Phase 1: Polymarket Integration**
- [ ] Tạo `retrieval/adapters/polymarket.py`
- [ ] Thêm Polymarket config vào `config.py`
- [ ] Thêm vào `registry.py`
- [ ] Unit tests
- [ ] Integration tests
- [ ] Update docs

### **Phase 2: Engagement Scoring**
- [ ] Cập nhật `scoring.py`
- [ ] Thêm engagement fields vào `SearchResult`
- [ ] Cập nhật `merger.py`
- [ ] Tests

### **Phase 3: USPTO Patents**
- [ ] Tạo `retrieval/adapters/uspto.py`
- [ ] Config
- [ ] Registry
- [ ] Tests

### **Phase 4: Careers Pages**
- [ ] Tạo `retrieval/adapters/careers.py`
- [ ] Config
- [ ] Registry
- [ ] Tests

### **Phase 5: TikTok/Instagram**
- [ ] Tạo adapters
- [ ] ScrapeCreators API integration
- [ ] Config
- [ ] Tests

### **Phase 6: Secret Signals**
- [ ] Patent analysis module
- [ ] Jobs signal detection
- [ ] Integration with research
- [ ] Tests

---

## 🔗 TÀI LIỆU THAM KHẢO

- [Last30Days GitHub](https://github.com/mvanhorn/last30days-skill)
- [Last30Days SKILL.md](https://github.com/mvanhorn/last30days-skill/blob/main/skills/last30days/SKILL.md)
- [Last30Days CONFIGURATION.md](https://github.com/mvanhorn/last30days-skill/blob/main/CONFIGURATION.md)
- [Polymarket Gamma API](https://gamma-api.polymarket.com)
- [ScrapeCreators API](https://api.scrapecreators.com)
- [USPTO API](https://developer.uspto.gov)
- [MetaSearch Current Adapters](api/app/retrieval/adapters/)
- [MetaSearch Scoring](api/app/retrieval/scoring.py)
- [MetaSearch Merger](api/app/retrieval/merger.py)

---

*Document version: 1.0*
*Last updated: 2026-11-20*
*Author: MetaSearch Team*
