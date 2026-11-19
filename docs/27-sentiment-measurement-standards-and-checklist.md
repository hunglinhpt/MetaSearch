# 27 — Tiêu chuẩn Đo đạc Tâm lý Thị trường (Market Sentiment Standards), Công thức Định lượng & 7-Pillar Checklist

Tài liệu này tổng hợp kết quả thẩm định từ **20+ nguồn chuẩn học thuật, cơ quan quản lý, quỹ định lượng và cổng dữ liệu tài chính quốc tế & Việt Nam** về đo đạc tâm lý thị trường (*Investor & Market Sentiment*), đối chiếu trực tiếp với kiến trúc **Omni-Channel Bilingual Equity Sentiment Engine (`omni_channel_equity_sentiment_v2`)** trong `api/app/retrieval/sentiment.py` và module định lượng `api/app/retrieval/sentiment_quant.py`.

> **Nguyên tắc Zero-Fabrication**: Mọi chỉ số định lượng (OHLCV, MA125/MA50/MA200, RSI-14, MFI-14, CMF-21, TRIN, McClellan RANA/NYMO, VIX/VIX3M, DIX, AAII/II/NAAIM, S-Score, BCTC) chỉ được tính khi có nến thật, proxy ngoại sinh hoặc cờ `fetch_market_data=true` trả về dữ liệu xác thực từ VNDIRECT/SSI/TCBS/FireAnt/Yahoo Finance. Khi thiếu dữ liệu đầu vào, trường tương ứng giữ nguyên `null`, tuyệt đối không nội suy hay bịa số.

---

## 1. Thẩm định Nguồn Chuẩn Quốc tế & Việt Nam (Tích hợp Toàn diện)

### 1.1 Finnhub — Social Sentiment, Earnings Call Live/Transcripts & Filings/Insider Sentiment
- **Nguồn tham chiếu**: `https://finnhub.io/docs/api/earnings-call-live-api`, `/stock/social-sentiment`, `/stock/filings-sentiment`, `/stock/insider-sentiment`.
- **Chuẩn đo lường cốt lõi**:
  1. **Social Sentiment (`reddit`, `twitter`)**: Tách biệt `mention` (tần suất nhắc tới), `positiveScore`, `negativeScore`, và `score` ròng theo từng khung thời gian để phát hiện đột biến chú ý (*Buzz / Relative Mention Velocity*).
  2. **Earnings Call & Filings Sentiment**: Áp dụng từ điển tài chính chuyên biệt **Loughran & McDonald (2011)** trên báo cáo 10-K/10-Q và biên bản họp ĐHĐCĐ / Earnings Call (`transcript`, `guidance`, `Q&A`), tách bạch **rủi ro pháp lý / kiểm toán / bất định (`uncertainty & litigation`)** khỏi cảm xúc tích cực/tiêu cực thông thường.
  3. **Insider Sentiment (MSPR — Monthly Share Purchase Ratio)**: Đo tỷ lệ mua/bán ròng của cổ đông nội bộ, HĐQT, Ban Giám đốc (`Form 4`, thông báo giao dịch nội bộ trên HOSE/HNX).
- **Áp dụng vào MetaSearch (`sentiment.py`)**:
  - Mở rộng Layer 1 fan-out bao phủ `giao dịch nội bộ`, `transcript`, `insider`, `guidance`.
  - Bổ sung bộ phân loại `insider_signal` (`NET_BUYING`, `NET_SELLING`, `MIXED`, `NONE`) và chỉ số `uncertainty_and_litigation_score` ($0.0 \dots 1.0$).

### 1.2 Nghiên cứu Học thuật — *(PDF) Investor Sentiment and The Stock Market* (ResearchGate 379351322, Baker & Wurgler 2006, Tetlock 2007, Loughran-McDonald 2011)
- **Nguồn tham chiếu**: `https://www.researchgate.net/publication/379351322_Investor_Sentiment_and_The_Stock_Market`
- **Chuẩn đo lường cốt lõi**:
  1. **Phân tách Direct vs. Indirect Sentiment**:
     - *Direct Sentiment*: Khảo sát, truyền thông, diễn đàn, mạng xã hội.
     - *Indirect Sentiment (Market Proxies)*: Thanh khoản/vòng quay vốn (*turnover*), độ rộng thị trường (*advance/decline*), biến động (*volatility*), dư nợ ký quỹ (*margin debt*).
  2. **Xử lý Phủ định & Ngữ cảnh Tài chính (Negation & Domain Context Guard)**:
     - Từ điển thông thường dễ chấm điểm sai khi gặp cấu trúc phủ định (ví dụ: *"không đạt lợi nhuận kỷ lục"*, *"chưa thể vượt đỉnh"*, *"failed to beat estimates"*).
  3. **Hiệu ứng Ngược chiều ở Cực trị (Contrarian Extremes — Baker & Wurgler)**:
     - Khi đám đông cá nhân hưng phấn tột độ trên các cổ phiếu đầu cơ nhưng thiếu dòng tiền tổ chức nâng đỡ, tỷ suất sinh lời kỳ vọng trong tương lai mang dấu âm (`CONTRARIAN_BEARISH_WARNING`).
- **Áp dụng vào MetaSearch (`sentiment.py`)**:
  - Tích hợp bộ lọc phủ định song ngữ `_is_negated_match` (`_NEGATION_PREFIX_RE`) kiểm tra cửa sổ ngữ cảnh đứng trước thuật ngữ để đảo chiều trọng số (`-0.75 * weight`) khi có từ phủ định (`không`, `chưa`, `bác bỏ`, `not`, `failed to`, `unlikely to`...).

### 1.3 Investopedia — Market Sentiment & Technical Breadth Indicators
- **Nguồn tham chiếu**: `https://www.investopedia.com/terms/m/marketsentiment.asp`
- **Chuẩn đo lường cốt lõi**:
  - Neo cảm xúc vào các chỉ báo kỹ thuật định lượng:
    - **VIX (CBOE Volatility Index)**: Thước đo nỗi sợ hãi và biến động kỳ vọng.
    - **High-Low Index**: Tỷ lệ cổ phiếu lập đỉnh 52 tuần (`52-week high`) so với đáy 52 tuần (`52-week low`).
    - **Bullish Percent Index (BPI)**: Độ rộng thị trường và tỷ lệ cổ phiếu có tín hiệu mua.
    - **Moving Averages (MA50 / MA200)**: Xu hướng trung và dài hạn (`Golden Cross`, `Death Cross`, `vượt MA200`, `thủng MA200`).
- **Áp dụng vào MetaSearch (`sentiment.py`)**:
  - Bổ sung nhóm từ khóa kỹ thuật & độ rộng (`_MOMENTUM_MA_TERMS`, `_BREADTH_LIQUIDITY_TERMS`) vào Pillar 3 và Pillar 4 của bảng kiểm 7 trụ cột.

### 1.4 VietstockFinance — Tâm lý Thị trường (Market Psychology)
- **Nguồn tham chiếu**: `https://finance.vietstock.vn/market-psychology.htm?languageid=2`
- **Chuẩn đo lường cốt lõi**:
  - Theo dõi kỳ vọng dòng tiền nội địa qua **Đồng hồ Tâm lý Thị trường 5 trạng thái** gắn với thanh khoản so với trung bình 20 phiên (`thanh khoản đột biến`, `cạn cung`, `tiền vào bắt đáy`, `xanh vỏ đỏ lòng`):
    1. `BI_QUAN_QUA_MUC` ($0 \dots 24$)
    2. `THAN_TRONG_SO_HAI` ($25 \dots 44$)
    3. `TRUNG_LAP_PHAN_VAN` ($45 \dots 55$)
    4. `LAC_QUAN_HUNG_PHAN` ($56 \dots 74$)
    5. `HUNG_PHAN_QUA_MUC` ($75 \dots 100$)
- **Áp dụng vào MetaSearch (`sentiment.py`)**:
  - Đưa `site:finance.vietstock.vn` vào Layer 2 fan-out và chuẩn hóa trường `vietstock_psychology_state` trong `benchmark_standards`.

### 1.5 SentimenTrader — Smart Money / Dumb Money Confidence & Optix (0–100)
- **Nguồn tham chiếu**: `https://sentimentrader.com/`
- **Chuẩn đo lường cốt lõi**:
  1. **Smart Money Confidence ($0 \dots 100$) vs. Dumb Money Confidence ($0 \dots 100$)**:
     - *Smart Money* (`TIER_1` + `TIER_2`: định chế, khối ngoại, tự doanh, nội bộ) thường mua khi thị trường hoảng loạn và bán khi hưng phấn.
     - *Dumb Money* (`TIER_3` + `TIER_4`: nhà đầu tư cá nhân nhỏ lẻ, FOMO hội nhóm) thường mua đuổi ở đỉnh (`> 70`) và bán tháo ở đáy (`< 30`).
  2. **Optix Component-Bounded Normalization**:
     - Chuẩn hóa từng thành phần về thang `[0, 100]` trước khi tổng hợp để một cú spike nhiễu trên mạng xã hội không thể làm lệch toàn bộ chỉ số tổng hợp.
- **Áp dụng vào MetaSearch (`sentiment.py`)**:
  - Tính toán trực tiếp `smart_money_confidence`, `dumb_money_confidence`, `smart_dumb_spread`, và tín hiệu ngược chiều `contrarian_signal` (`CONTRARIAN_BULLISH_OPPORTUNITY`, `CONTRARIAN_BEARISH_WARNING`, `TREND_ALIGNED`, `NEUTRAL`).

### 1.6 Sentimentick — Real-Time Social Buzz Velocity & Pre-Breakout Setup Isolation
- **Nguồn tham chiếu**: `https://www.sentimentick.com/`
- **Chuẩn đo lường cốt lõi**:
  - Đo gia tốc chú ý (*Buzz Velocity*) bằng tỷ lệ thảo luận trong `< 24h` (`BREAKING_6H` + `INTRADAY_24H`) so với nền lịch sử (`SHORT_3D` + `WEEKLY_7D`), kết hợp phát hiện sớm giai đoạn tích lũy ngầm trước khi giá bứt phá.
- **Áp dụng vào MetaSearch (`sentiment.py`)**:
  - Phân loại `buzz_velocity` thành 4 mức: `VIRAL_SURGE`, `ELEVATED`, `NORMAL`, `DORMANT`.

### 1.7 CNN Fear & Greed Index — 7-Pillar Composite Methodology (0–100)
- **Nguồn tham chiếu**: `https://edition.cnn.com/markets/fear-and-greed`
- **Chuẩn đo lường cốt lõi**:
  - Hợp nhất **7 chỉ báo bình đẳng (`1/7` mỗi chỉ báo)** về thang `0..100` (`EXTREME_FEAR` $0\text{–}24$, `FEAR` $25\text{–}44$, `NEUTRAL` $45\text{–}55$, `GREED` $56\text{–}74$, `EXTREME_GREED` $75\text{–}100$): S&P 500 vs **MA 125 phiên**, VIX vs **MA 50 phiên**, Junk Bond Yield vs Investment Grade spread, Stocks vs Treasuries 20D return, NYSE McClellan Volume Summation Index, Net New 52W Highs vs Lows, và CBOE Put/Call 5D.

### 1.8 Khảo sát Tổ chức & Cá nhân (AAII, Investors Intelligence, NAAIM)
- **AAII Investor Sentiment Survey (`aaii.com`)**:
  - Công thức: $\text{Bull-Bear Spread} = \text{Bullish\%} - \text{Bearish\%}$ (trung bình lịch sử $\approx +6.5$ điểm %; cực trị tham lam $> +30 \dots +40$, đáy hoảng loạn $< -20 \dots -25$).
- **Investors Intelligence (II) Advisors Sentiment**:
  - Công thức: $\text{II Ratio} = \frac{\text{Bullish\%}}{\text{Bearish\%}}$ (mức chuẩn $\approx 1.25$; $> 3.0$ hưng phấn quá mức, $< 1.0$ đáy cấu trúc).
- **NAAIM Exposure Index (`naaim.org`)**:
  - Trung bình tỷ trọng cổ phiếu của các nhà quản lý quỹ chủ động trong dải $[-200\%, +200\%]$ ($> 90\%$ căng margin long, $< 40\%$ phòng thủ tiền mặt).

### 1.9 BofA Bull & Bear Indicator & Citi Panic/Euphoria (Levkovich)
- **BofA Bull & Bear Indicator (`0..10`)**: Hợp nhất định vị FMS, CFTC hedge fund futures, dòng vốn cổ phiếu/trái phiếu, kỹ thuật tín dụng và độ rộng toàn cầu (`MA50/MA200`); tín hiệu bán ngược chiều $> 8.0$, mua ngược chiều $< 2.0$. Trong `sentiment_quant.py` trả về `bofa_bull_bear_analog_0_10` khi có đủ $\ge 2$ nhóm proxy định lượng.
- **Citi Levkovich (Panic/Euphoria)**: Ngưỡng tham chiếu học thuật Euphoria $\ge +0.41$ vs. Panic $\le -0.17$.

### 1.10 CBOE Options, Volatility Term Structure & Tail Risk (`cboe.com`)
- **CBOE Equity / Total Put-Call Ratio**: `< 0.50` tự mãn hưng phấn vs `> 1.00..1.15` phòng hộ cực đoan / tạo đáy.
- **Cấu trúc kỳ hạn VIX (`VIX / VIX3M`)**:
  - `> 1.00`: `BACKWARDATION_STRESS` (hoảng loạn ngắn hạn vượt dài hạn — Pillar 5 tự gắn cờ `WARNING`/`BEARISH`).
  - `0.90..1.00`: `ELEVATED_CONTANGO`; `< 0.90`: `CALM_CONTANGO`.
- **CBOE SKEW Index**: `110–130` bình thường, $\ge 140$ cảnh báo tổ chức phòng hộ rủi ro đuôi (*tail-risk hedging*).

### 1.11 Công thức Kỹ thuật & Độ rộng Thị trường Định lượng (`sentiment_quant.py`)
- **Arms Index (`TRIN`)**: $\text{TRIN} = \frac{\text{Advancing} / \text{Declining}}{\text{AdvVol} / \text{DecVol}}$ (`< 0.70` tích lũy mạnh / bullish, `> 1.80` áp lực bán nặng / heavy selling; thang chuẩn hóa tuyến tính trong `[0.50, 2.00]` đảo chiều về `0..100`).
- **McClellan Ratio-Adjusted Net Advances (`RANA`) & Oscillator (`NYMO`)**:
  $$\text{RANA} = \frac{\text{Adv} - \text{Dec}}{\text{Adv} + \text{Dec}} \times 1000, \qquad \text{NYMO} = \text{EMA}_{19}(\text{RANA}, \alpha=0.10) - \text{EMA}_{39}(\text{RANA}, \alpha=0.05)$$
- **High-Low Breadth %**: $\frac{\text{NewHigh}_{52W}}{\text{NewHigh}_{52W} + \text{NewLow}_{52W}} \times 100$.
- **Wilder RSI(14)** (`calc_wilder_rsi`), **Money Flow Index (`MFI-14`)**:
  $$TP = \frac{H + L + C}{3}, \quad RMF = TP \times V, \quad \text{MFI}_{14} = 100 - \frac{100}{1 + \text{PosMF}_{14} / \text{NegMF}_{14}}$$
- **Chaikin Money Flow (`CMF-21`)**:
  $$MFM = \frac{(C - L) - (H - C)}{H - L}, \quad \text{CMF}_{21} = \frac{\sum_{t=1}^{21} MFM_t \cdot V_t}{\sum_{t=1}^{21} V_t} \in [-1, +1]$$
- **Tỷ lệ biến động thực tế 20D/60D (`vol_ratio_20_60`)**: Log-return annualized $\sigma_{20} / \sigma_{60}$ (phiên bản tương đương theo mã cổ phiếu của `VIX / VIX_MA50`).

### 1.12 Dark Pool (`SqueezeMetrics DIX/GEX`) & Ký quỹ (`FINRA Margin Debt`)
- **Dark Pool Index (`DIX`)**: $\text{DIX} = \frac{\sum \text{Price} \times \text{OffExchangeShortDollarVol}}{\sum \text{Price} \times \text{TotalOffExchangeDollarVol}} \times 100\%$ ($\ge 45\%$ cho thấy tổ chức âm thầm gom qua dark pool bất chấp biến động ngắn hạn).
- **FINRA Margin Debt YoY%**: $\frac{\text{Debt}_{\text{curr}} - \text{Debt}_{\text{prev\_year}}}{\text{Debt}_{\text{prev\_year}}} \times 100$.

### 1.13 Dữ liệu Thay thế Quốc tế, S-Score & Học thuật NLP
- **Context Analytics / StockTwits S-Score**: $S = \frac{S_{\text{weighted, 24h}} - \mu_{\text{20d}}}{\sigma_{\text{20d}}}$ (tự động nâng `buzz_velocity` lên `VIRAL_SURGE` khi $S \ge 2.0$, `ELEVATED` khi $S \ge 1.0$).
- **Alpha Vantage `NEWS_SENTIMENT`**, **FMP** (`stocktwitsSentiment` & Form 4 $\text{MSPR} = \frac{\text{Acquired} - \text{Disposed}}{\text{Acquired} + \text{Disposed}}$), **TipRanks Smart Score**, **WhaleWisdom 13F**, **QuiverQuant**, **Unusual Whales**, **SwaggyStocks**, **ApeWisdom**, **FINVIZ**, **Barchart**, **Alternative.me Crypto Fear & Greed**.
- **Loughran-McDonald Sublinear TF-IDF (`idf_weights`)**: Áp dụng mượt `[0.50, 1.25]` khi số tài liệu $\ge 3$ để hạ trọng số các cụm từ lặp lại đồng loạt kiểu spam/boilerplate và tôn trọng tín hiệu xúc tác đặc thù.
- **Tetlock (2007) Media Pessimism Ratio**: Tỷ trọng khối lượng tiêu cực có trọng số trên lớp `TIER_1` + `TIER_2` ($\in [0, 1]$).
- **Consensus Polarization Index**: $1 - \frac{|\sum w_i s_i|}{\sum w_i |s_i|} \in [0, 1]$ (`0` đồng thuận tuyệt đối, `1` phân cực giằng co phe mua/bán) cùng **Source Diversity Entropy**.

### 1.14 Cổng Dữ liệu Định lượng Việt Nam
- Mở rộng query fan-out Layer 2 & phân loại `TIER_2` cho **FiinTrade (`fiintrade.vn`)**, **Wichart (`wichart.vn`)**, **Simplize (`simplize.vn`)**, **SSI iBoard (`iboard.ssi.com.vn`)**, **VNDIRECT dstock/dchart**, **24HMoney (`24hmoney.vn`)**; hỗ trợ trực tiếp `vn_foreign_net_value_bil`, `vn_prop_net_value_bil`, bội số thanh khoản `volume_ratio_20d` so với trung bình 20 phiên, và lấy OHLCV/FA thật khi bật `fetch_market_data=true`.

---

## 2. Bảng kiểm 7 Trụ cột Đo đạc Sentiment Chuẩn hóa (`seven_pillar_checklist`)

Mỗi phản hồi từ `POST /v1/market/sentiment` (và MCP tool `equity_sentiment`) trả về đối tượng `benchmark_standards` gồm đầy đủ **7 trụ cột kiểm định** (tự động hòa trộn *text NLP* + *công thức định lượng thật* khi có `ohlcv_bars`, `market_proxies`, hoặc `fetch_market_data=true`):

| # | `pillar_id` | Tên trụ cột | Nguồn chuẩn tham chiếu | Nội dung đo lường & công thức |
|---|---|---|---|---|
| 1 | `filings_earnings_insider` | Official Filings, Earnings Call & Insider (MSPR) | Finnhub Filings/Transcripts/Insider, Form 4 & SEC/HOSE | CBTT, BCTC, Earnings Call guidance, MSPR giao dịch nội bộ, kết hợp FA thật (`pe`, `pb`, `roe`, hỗ trợ nguồn `roe_pct`). |
| 2 | `institutional_smart_money` | Institutional Research & Smart Money Flow | SentimenTrader, SqueezeMetrics DIX/GEX & VN Foreign/Prop Flow | Báo cáo CTCK, DIX/GEX dark pool, dòng tiền ròng khối ngoại & tự doanh (`TIER_2`). |
| 3 | `momentum_and_moving_averages` | Price Momentum & Moving Averages (MA50/MA125/MA200/RSI-14) | Investopedia & CNN Fear & Greed Momentum | Vượt/thủng `MA50`/`MA125`/`MA200`, `RSI(14)` Wilder, tỷ suất sinh lời 20 phiên. |
| 4 | `breadth_strength_and_liquidity` | Market Breadth (TRIN/McClellan/52W H-L) & Flow (MFI-14/CMF-21) | Vietstock/FiinTrade Psychology & CNN Breadth | Vị thế 52W High/Low, `TRIN`, `McClellan RANA/NYMO`, `MFI(14)`, `CMF(21)`, `volume_ratio_20d`. |
| 5 | `volatility_options_and_leverage` | Volatility (VIX/VIX3M/SKEW), Options Put/Call & Margin/Audit Risk | CBOE Options/Vol, FINRA Margin & Loughran-McDonald | `vol_ratio_20_60`, `VIX/VIX3M`, `CBOE SKEW`, `Put/Call`, `Margin Debt YoY%`, rủi ro kiểm toán/pháp lý. |
| 6 | `retail_social_and_buzz_velocity` | Retail Crowd, Surveys (AAII/II/NAAIM) & Buzz S-Score | AAII/NAAIM, Sentimentick, StockTwits S-Score & Finnhub Social | Tâm lý đám đông F319/FireAnt/Facebook/X/Reddit/StockTwits, `AAII Spread`, `II Ratio`, `S-Score`. |
| 7 | `anti_seeding_and_data_integrity` | Social Manipulation, Bot/VIP Seeding & Rumor Audit | ResearchGate 379351322 & MetaSearch Phase 2B | Kiểm toán chống lùa gà copy-paste (`COORDINATED_SOCIAL_SEEDING`) và tin đồn chưa kiểm chứng (`UNVERIFIED_RUMOR_SPIKE`). |

---

## 3. Hợp đồng `nlp_telemetry` & `quantitative_telemetry`

- **`benchmark_standards.nlp_telemetry`**: Trả về `tfidf_corpus_weighting`, `corpus_doc_count`, `tetlock_media_pessimism`, `consensus_polarization`, và `source_diversity_entropy`.
- **`benchmark_standards.quantitative_telemetry`**: Chỉ có giá trị (hoặc không `null`) khi nhận được `ohlcv_bars` thật, `market_proxies`, hoặc `fetch_market_data=true` lấy được dữ liệu từ VNDIRECT/SSI/TCBS/FireAnt/Yahoo Finance:
  - `ohlcv`: `last_close`, `return_1d_pct`, `return_20d_pct`, `pct_vs_ma50`, `pct_vs_ma125`, `pct_vs_ma200`, `range_pos_52w_pct`, `rsi_14`, `mfi_14`, `cmf_21`, `volume_ratio_20d`, `realized_vol_20d_pct`, `realized_vol_60d_pct`, `vol_ratio_20_60` kèm `source_name`/`source_url`/`as_of`.
  - `market_proxies`: `aaii_bull_bear_spread`, `ii_bull_bear_ratio`, `naaim_exposure_pct`, `arms_trin`, `mcclellan_rana`, `mcclellan_oscillator`, `high_low_breadth_pct`, `vix_term_ratio`, `vix_term_regime`, `cboe_put_call_ratio`, `cboe_skew`, `dix_pct`, `gex_billions`, `margin_debt_yoy_pct`, `credit_spread_bps`, `social_s_score`, `insider_mspr`, `bofa_bull_bear_analog_0_10`.
  - `fundamentals`: `pe`, `pb`, `roe`, `revenue_growth_yoy`, `profit_growth_yoy`, `debt_to_equity`, `market_cap` kèm `source`/`source_url`/`as_of`.

## 4. Bổ sung v4.0 (Live Keyless Feeds, SEntFiN Attribution & S-Score 20d)

- **Nguồn live keyless bổ sung**: CNN Fear & Greed (`dataviz.cnn.io`), CBOE Daily Put/Call (`cdn.cboe.com`), FRED HY OAS (`fred.stlouisfed.org`), CafeF flows (`s.cafef.vn`), SSI iBoard orderflow & HOSE breadth (`iboard-query.ssi.com.vn`), DNSE OHLCV fallback (`services.entrade.com.vn`), Simplize broker consensus (`api2.simplize.vn`), kích hoạt khi `fetch_market_data=true` & `include_institutional_feeds=true` (tự động tắt trên BYO `retrieved_documents` trừ khi caller yêu cầu rõ).
- **SEntFiN & Lịch sử 20 phiên (`sentiment_snapshots`, migration `0011`)**: Phân tách polarity cấp câu theo mã mục tiêu + `tanh` saturation, lưu snapshot hằng ngày vào DB để tính 20-day `s_score` / `sv_score` chuẩn StockTwits (trả `available=false` khi `< 5` ngày lịch sử, tuyệt đối không nội suy).
- **Benchmark định lượng**: Bộ test chuẩn hóa `api/evals/sentiment_golden.json` + `api/scripts/eval_sentiment.py` kiểm định đồng thời Macro-F1 ($\ge 0.85$), collision rejection (`VNM`/`GAS`/`SSI`/`POW`), và SEntFiN target attribution.
