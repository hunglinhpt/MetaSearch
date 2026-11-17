"""Omni-Channel Bilingual Equity Sentiment Engine (VI / EN) — module v2.

Implements the 3-phase federated sentiment discovery pipeline:
1. Phase 1 — Omni-Channel Query Fan-Out across 5 network layers (Exchanges/Filings,
   Institutional/Financial Media, Vietnamese Social Ecosystems, Global Cashtag/Social
   Streams, and Video/KOL Channels). Bypasses search if ``retrieved_documents`` are
   already supplied in context.
2. Phase 2 — Cross-Network Disambiguation & Anti-Seeding Filter:
   - Strict entity collision filtering (e.g., VNM=Vinamilk vs VanEck ETF,
     GAS=PV Gas vs natural gas commodity, SSI=SSI Securities vs US Social Security,
     FPT=FPT Corp vs First Trust ETF, POW=PV Power vs Proof-of-Work).
   - Coordinated social seeding detector (collapses duplicate/VIP-room copy-paste spam
     into 1 single TIER_4 signal and raises ``COORDINATED_SOCIAL_SEEDING``).
   - Unverified social rumor discounting (excludes uncorroborated viral rumors from
     factual drivers while counting them in retail crowd emotion).
   - Mathematical weighting: ``Effective_Weight = Tier_Weight * Time_Decay``.
3. Phase 3 — Bilingual Financial Lexicon & Regime Classification:
   - Scores Vietnamese & English market slang/catalysts.
   - Classifies ``market_regime`` (STEALTH_ACCUMULATION, MOMENTUM_MARKUP,
     EUPHORIC_DISTRIBUTION_TRAP, PANIC_CAPITULATION, EVENT_DRIVEN_BINARY,
     LOW_SIGNAL_NOISE).
   - Produces a deterministic, strictly grounded JSON payload with verbatim quotes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from app.retrieval.sentiment_quant import (
    FundamentalSnapshotInput,
    MarketProxiesInput,
    OhlcvBarInput,
    compute_consensus_polarization,
    compute_market_proxy_metrics,
    compute_ohlcv_sentiment_metrics,
    compute_source_diversity_entropy,
    compute_tetlock_pessimism_ratio,
    compute_tfidf_idf_map,
)
from app.retrieval.vn_tickers import load_vn_tickers

ExchangeCode = Literal["HOSE", "HNX", "UPCOM", "NASDAQ", "NYSE", "OTHER"]
SentimentLabel = Literal["STRONG_BULLISH", "BULLISH", "NEUTRAL", "BEARISH", "STRONG_BEARISH"]
MarketRegime = Literal[
    "STEALTH_ACCUMULATION",
    "MOMENTUM_MARKUP",
    "EUPHORIC_DISTRIBUTION_TRAP",
    "PANIC_CAPITULATION",
    "EVENT_DRIVEN_BINARY",
    "LOW_SIGNAL_NOISE",
]
Momentum24h = Literal["IMPROVING", "STABLE", "DETERIORATING", "UNKNOWN"]
TierCode = Literal["TIER_1", "TIER_2", "TIER_3", "TIER_4"]
FreshnessBucket = Literal["BREAKING_6H", "INTRADAY_24H", "SHORT_3D", "WEEKLY_7D", "STALE"]
PolarityCode = Literal["POSITIVE", "NEGATIVE", "NEUTRAL"]
LangCode = Literal["VI", "EN"]
FearGreedZone = Literal[
    "EXTREME_FEAR", "FEAR", "NEUTRAL", "GREED", "EXTREME_GREED", "INSUFFICIENT_DATA"
]
VietstockPsychologyState = Literal[
    "BI_QUAN_QUA_MUC",
    "THAN_TRONG_SO_HAI",
    "TRUNG_LAP_PHAN_VAN",
    "LAC_QUAN_HUNG_PHAN",
    "HUNG_PHAN_QUA_MUC",
    "THIEU_DU_LIEU",
]
ContrarianSignal = Literal[
    "CONTRARIAN_BULLISH_OPPORTUNITY",
    "CONTRARIAN_BEARISH_WARNING",
    "TREND_ALIGNED",
    "NEUTRAL",
]
BuzzVelocity = Literal["VIRAL_SURGE", "ELEVATED", "NORMAL", "DORMANT"]
InsiderSignal = Literal["NET_BUYING", "NET_SELLING", "MIXED", "NONE"]
PillarStatus = Literal["BULLISH", "BEARISH", "NEUTRAL", "WARNING", "INSUFFICIENT_DATA"]

TIER_WEIGHTS: dict[TierCode, float] = {
    "TIER_1": 1.00,
    "TIER_2": 0.75,
    "TIER_3": 0.35,
    "TIER_4": 0.15,
}

TIME_DECAY_WEIGHTS: dict[FreshnessBucket, float] = {
    "BREAKING_6H": 1.00,
    "INTRADAY_24H": 0.85,
    "SHORT_3D": 0.60,
    "WEEKLY_7D": 0.35,
    "STALE": 0.10,
}

# Curated global equity catalog for high-precision resolution of major US/global tickers.
_GLOBAL_EQUITIES: dict[str, tuple[str, ExchangeCode]] = {
    "NVDA": ("NVIDIA Corporation", "NASDAQ"),
    "TSLA": ("Tesla, Inc.", "NASDAQ"),
    "AAPL": ("Apple Inc.", "NASDAQ"),
    "MSFT": ("Microsoft Corporation", "NASDAQ"),
    "GOOGL": ("Alphabet Inc.", "NASDAQ"),
    "GOOG": ("Alphabet Inc.", "NASDAQ"),
    "AMZN": ("Amazon.com, Inc.", "NASDAQ"),
    "META": ("Meta Platforms, Inc.", "NASDAQ"),
    "AMD": ("Advanced Micro Devices, Inc.", "NASDAQ"),
    "AVGO": ("Broadcom Inc.", "NASDAQ"),
    "NFLX": ("Netflix, Inc.", "NASDAQ"),
    "INTC": ("Intel Corporation", "NASDAQ"),
    "BRK.A": ("Berkshire Hathaway Inc.", "NYSE"),
    "BRK.B": ("Berkshire Hathaway Inc.", "NYSE"),
    "JPM": ("JPMorgan Chase & Co.", "NYSE"),
    "V": ("Visa Inc.", "NYSE"),
    "TSM": ("Taiwan Semiconductor Manufacturing", "NYSE"),
    "BABA": ("Alibaba Group Holding", "NYSE"),
}

# Anti-collision blacklist phrases per ticker (lowercase substring match).
# When a document matches a collision phrase without explicitly naming the target
# corporate entity, it is immediately purged in Phase 2A.
_TICKER_COLLISION_RULES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "VNM": (
        ("vaneck", "vietnam etf", "market vectors", "nysearca:vnm", "bats:vnm", "etf vnm"),
        ("vinamilk", "sữa việt nam", "ctcp sữa", "hose:vnm", "hsx:vnm"),
    ),
    "GAS": (
        (
            "henry hub",
            "nymex",
            "natural gas futures",
            "gasoline price",
            "u.s. natural gas",
            "lng futures",
            "cng price",
        ),
        ("pv gas", "pvgas", "khí việt nam", "tổng công ty khí", "hose:gas", "cổ phiếu gas"),
    ),
    "SSI": (
        (
            "social security",
            "supplemental security income",
            "ssa.gov",
            "disability benefits",
            "medicare",
        ),
        ("chứng khoán ssi", "ssi securities", "ssi research", "nguyễn duy hưng", "hose:ssi"),
    ),
    "FPT": (
        (
            "first trust",
            "floating point",
            "fpt etf",
            "female pelvic",
            "fast packet",
        ),
        ("fpt corp", "ctcp fpt", "tập đoàn fpt", "fpt software", "fpt telecom", "trương gia bình"),
    ),
    "POW": (
        (
            "proof of work",
            "proof-of-work",
            "prisoner of war",
            "prisoners of war",
            "bitcoin mining pow",
        ),
        (
            "pv power",
            "điện lực dầu khí",
            "tổng công ty điện lực dầu khí",
            "hose:pow",
            "cổ phiếu pow",
        ),
    ),
}

# Domain classification sets for Phase 2C Tier assignment
_TIER_1_DOMAINS: frozenset[str] = frozenset(
    {
        "hsx.vn",
        "hnx.vn",
        "ssc.gov.vn",
        "vsd.vn",
        "vsdc.vn",
        "sbv.gov.vn",
        "moit.gov.vn",
        "mof.gov.vn",
        "chinhphu.vn",
        "sec.gov",
        "federalreserve.gov",
        "treasury.gov",
        "finra.org",
        "cboe.com",
        "fred.stlouisfed.org",
        "aaii.com",
        "naaim.org",
    }
)

_TIER_2_DOMAINS: frozenset[str] = frozenset(
    {
        "cafef.vn",
        "vietstock.vn",
        "vneconomy.vn",
        "tinnhanhchungkhoan.vn",
        "ndh.vn",
        "thesaigontimes.vn",
        "baodautu.vn",
        "diendandoanhnghiep.vn",
        "fiintrade.vn",
        "wichart.vn",
        "simplize.vn",
        "iboard.ssi.com.vn",
        "dstock.vndirect.com.vn",
        "ssi.com.vn",
        "vndirect.com.vn",
        "hsc.com.vn",
        "vietcap.com.vn",
        "vcsc.com.vn",
        "mbs.com.vn",
        "tcbs.com.vn",
        "vcbs.com.vn",
        "bsc.com.vn",
        "fpts.com.vn",
        # PASS-2 audit P1-2: absorbed from the removed keyword-substring
        # fallback — registered press only, never keyword matching.
        "tuoitre.vn",
        "vnexpress.net",
        "dantri.com.vn",
        "thanhnien.vn",
        "baovanhoa.vn",
        "bloomberg.com",
        "reuters.com",
        "wsj.com",
        "ft.com",
        "seekingalpha.com",
        "finance.yahoo.com",
        "cnbc.com",
        "marketwatch.com",
        "barrons.com",
        "morningstar.com",
        "sentimentrader.com",
        "tipranks.com",
        "whalewisdom.com",
        "quiverquant.com",
        "barchart.com",
        "finviz.com",
        "alphavantage.co",
        "financialmodelingprep.com",
        "alternative.me",
        "finnhub.io",
        "edition.cnn.com",
        "investopedia.com",
        "squeezemetrics.com",
    }
)

_VN_SOCIAL_PLATFORM_MAP: dict[str, str] = {
    "facebook.com": "Facebook",
    "fb.com": "Facebook",
    "fireant.vn": "FireAnt",
    "f319.com": "F319",
    "voz.vn": "Voz",
    "t.me": "Telegram",
    "telegram.me": "Telegram",
    "threads.net": "Threads",
    "tiktok.com": "TikTok",
    "zalo.me": "Zalo",
    "24hmoney.vn": "24hMoney",
}

_GLOBAL_SOCIAL_PLATFORM_MAP: dict[str, str] = {
    "x.com": "X",
    "twitter.com": "X",
    "reddit.com": "Reddit",
    "stocktwits.com": "StockTwits",
    "substack.com": "Substack",
    "discord.com": "Discord",
    "apewisdom.io": "ApeWisdom",
    "swaggystocks.com": "SwaggyStocks",
    "unusualwhales.com": "UnusualWhales",
    "sentimentick.com": "Sentimentick",
}

# Promotional / VIP group seeding patterns (Phase 2B-1)
_SEEDING_PATTERNS: tuple[str, ...] = (
    "vào room vip",
    "room vip",
    "nhóm kín",
    "vào nhóm kín",
    "zalo vip",
    "room zalo",
    "phím hàng siêu cổ",
    "siêu cổ x2",
    "siêu cổ x3",
    "cam kết lợi nhuận",
    "vào bờ miễn phí",
    "link bio nhận mã",
    "inbox nhận mã",
    "join vip group",
    "vip signal group",
    "guaranteed 100% profit",
    "pump alert",
)

# Unverified rumor trigger phrases (Phase 2B-2)
_RUMOR_PATTERNS: tuple[str, ...] = (
    "tin đồn bắt bớ",
    "tin đồn bắt",
    "tin đồn khởi tố",
    "tin đồn chia thưởng khủng",
    "nghe đồn",
    "tin mật nội bộ",
    "tin hành lang",
    "rò rỉ tin mật",
    "tin đồn thâu tóm",
    "unverified rumor",
    "rumor has it",
    "whispers say",
    "unconfirmed leak",
)

# Phase 3 Bilingual Lexicon: (phrase, polarity_weight, canonical_tag)
_VI_BULLISH_LEXICON: tuple[tuple[str, float, str], ...] = (
    ("khối ngoại mua ròng", 0.45, "khối ngoại mua ròng"),
    ("nước ngoài mua ròng", 0.40, "khối ngoại mua ròng"),
    ("tự doanh gom hàng", 0.40, "tự doanh gom hàng"),
    ("tự doanh mua ròng", 0.40, "tự doanh mua ròng"),
    ("vượt đỉnh thanh khoản lớn", 0.45, "vượt đỉnh thanh khoản lớn"),
    ("vượt đỉnh 52 tuần", 0.40, "vượt đỉnh 52 tuần"),
    ("đỉnh 52 tuần", 0.35, "đỉnh 52 tuần"),
    ("vượt ma200", 0.35, "vượt MA200"),
    ("vượt ma50", 0.25, "vượt MA50"),
    ("bùng nổ theo đà", 0.40, "bùng nổ theo đà (FTD)"),
    ("lan tỏa dòng tiền", 0.30, "lan tỏa dòng tiền"),
    ("tiền vào bắt đáy", 0.35, "tiền vào bắt đáy"),
    ("cạn cung", 0.30, "cạn cung"),
    ("vượt đỉnh", 0.30, "vượt đỉnh"),
    ("thanh khoản đột biến", 0.25, "thanh khoản lớn"),
    ("cổ tức tiền mặt cao", 0.35, "cổ tức tiền mặt cao"),
    ("chia cổ tức tiền mặt", 0.30, "cổ tức tiền mặt"),
    ("game tăng vốn", 0.30, "game tăng vốn"),
    ("m&a chiến lược", 0.35, "M&A chiến lược"),
    ("nới room ngoại", 0.40, "nới room ngoại"),
    ("trúng thầu đầu tư công", 0.40, "trúng thầu đầu tư công"),
    ("trúng thầu", 0.30, "trúng thầu"),
    ("kéo trụ", 0.25, "kéo trụ"),
    ("lợi nhuận kỷ lục", 0.45, "lợi nhuận kỷ lục"),
    ("tăng trưởng bằng lần", 0.35, "tăng trưởng mạnh"),
    ("tăng trưởng vượt kỳ vọng", 0.40, "vượt kỳ vọng"),
    ("lãnh đạo đăng ký mua", 0.40, "nội bộ đăng ký mua"),
    ("chủ tịch đăng ký mua", 0.40, "nội bộ đăng ký mua"),
    ("nội bộ mua vào", 0.35, "nội bộ đăng ký mua"),
    ("cổ đông lớn mua vào", 0.35, "cổ đông lớn mua vào"),
    ("khuyến nghị mua", 0.35, "khuyến nghị MUA"),
    ("khả quan", 0.25, "triển vọng khả quan"),
    ("tích lũy chặt", 0.25, "tích lũy"),
    ("gom hàng", 0.25, "gom hàng"),
)

_VI_BEARISH_LEXICON: tuple[tuple[str, float, str], ...] = (
    ("khối ngoại xả ròng liên tục", -0.50, "khối ngoại xả ròng liên tục"),
    ("khối ngoại xả ròng", -0.40, "khối ngoại xả ròng"),
    ("khối ngoại bán ròng", -0.40, "khối ngoại bán ròng"),
    ("call margin", -0.50, "call margin"),
    ("giải chấp chéo", -0.55, "giải chấp chéo"),
    ("bán giải chấp", -0.45, "bán giải chấp"),
    ("lãnh đạo bán chui", -0.55, "lãnh đạo bán chui"),
    ("bán chui", -0.50, "bán chui"),
    ("lãnh đạo đăng ký bán", -0.35, "nội bộ đăng ký bán"),
    ("chủ tịch đăng ký bán", -0.35, "nội bộ đăng ký bán"),
    ("cổ đông lớn thoái vốn", -0.35, "cổ đông lớn thoái vốn"),
    ("đăng ký bán lớn", -0.35, "đăng ký bán lớn"),
    ("kiểm toán ngoại trừ", -0.50, "ý kiến kiểm toán ngoại trừ"),
    ("ý kiến ngoại trừ", -0.45, "ý kiến kiểm toán ngoại trừ"),
    ("diện cảnh báo", -0.40, "diện cảnh báo/kiểm soát"),
    ("diện kiểm soát", -0.45, "diện cảnh báo/kiểm soát"),
    ("hủy niêm yết", -0.55, "nguy cơ hủy niêm yết"),
    ("thanh tra", -0.40, "thanh tra"),
    ("khởi tố", -0.60, "khởi tố"),
    ("bắt tạm giam", -0.60, "bắt tạm giam"),
    ("phát hành pha loãng", -0.35, "phát hành pha loãng"),
    ("thủng ma200", -0.40, "thủng MA200"),
    ("gãy ma200", -0.40, "thủng MA200"),
    ("gãy nền", -0.35, "gãy nền giá"),
    ("đáy 52 tuần", -0.40, "thủng đáy 52 tuần"),
    ("xanh vỏ đỏ lòng", -0.30, "xanh vỏ đỏ lòng"),
    ("phân phối đỉnh", -0.40, "phân phối đỉnh"),
    ("thanh khoản sụt giảm", -0.20, "thanh khoản sụt giảm"),
    ("trắng bên mua", -0.50, "trắng bên mua"),
    ("nằm sàn", -0.40, "nằm sàn"),
    ("kéo xả", -0.45, "kéo xả"),
    ("úp bô", -0.45, "úp bô"),
    ("lùa gà", -0.45, "lùa gà"),
    ("đội lái xả", -0.45, "đội lái xả"),
    ("thua lỗ", -0.40, "thua lỗ"),
    ("sụt giảm mạnh", -0.35, "sụt giảm mạnh"),
)

_EN_BULLISH_LEXICON: tuple[tuple[str, float, str], ...] = (
    ("beat & raise", 0.50, "beat & raise"),
    ("beat and raise", 0.50, "beat & raise"),
    ("beats estimates", 0.40, "beats estimates"),
    ("raises guidance", 0.45, "raises guidance"),
    ("margin expansion", 0.40, "margin expansion"),
    ("institutional accumulation", 0.40, "institutional accumulation"),
    ("buyback authorization", 0.40, "buyback authorization"),
    ("share repurchase", 0.35, "share repurchase"),
    ("insider buying", 0.40, "insider buying"),
    ("form 4 buy", 0.40, "insider Form 4 buy"),
    ("cluster insider buying", 0.45, "cluster insider buying"),
    ("golden cross", 0.35, "golden cross"),
    ("above 200-day", 0.30, "above 200-day MA"),
    ("52-week high", 0.35, "52-week high"),
    ("positive breadth", 0.25, "positive breadth"),
    ("bullish percent", 0.25, "bullish percent"),
    ("short squeeze setup", 0.35, "short squeeze setup"),
    ("short squeeze", 0.30, "short squeeze"),
    ("gamma squeeze", 0.35, "gamma squeeze"),
    ("record revenue", 0.40, "record revenue"),
    ("upgrade to buy", 0.35, "upgrade to buy"),
    ("outperform", 0.30, "outperform"),
    ("strong buy", 0.35, "strong buy"),
    ("bullish breakout", 0.30, "bullish breakout"),
)

_EN_BEARISH_LEXICON: tuple[tuple[str, float, str], ...] = (
    ("guidance cut", -0.50, "guidance cut"),
    ("cuts guidance", -0.50, "guidance cut"),
    ("lowers guidance", -0.45, "guidance cut"),
    ("misses estimates", -0.40, "misses estimates"),
    ("multiple compression", -0.35, "multiple compression"),
    ("sec probe", -0.55, "SEC/regulatory probe"),
    ("regulatory probe", -0.50, "SEC/regulatory probe"),
    ("doj investigation", -0.55, "DOJ investigation"),
    ("going concern", -0.55, "going concern risk"),
    ("material weakness", -0.45, "material weakness"),
    ("restatement", -0.50, "financial restatement"),
    ("class action lawsuit", -0.45, "class action lawsuit"),
    ("insider dumping", -0.45, "insider dumping"),
    ("insider selling", -0.35, "insider selling"),
    ("form 4 sale", -0.35, "insider Form 4 sale"),
    ("debt covenant breach", -0.55, "debt covenant breach"),
    ("death cross", -0.40, "death cross"),
    ("below 200-day", -0.35, "below 200-day MA"),
    ("52-week low", -0.40, "52-week low"),
    ("high put/call", -0.30, "bearish put/call skew"),
    ("vix spike", -0.30, "volatility spike"),
    ("dead cat bounce", -0.40, "dead cat bounce"),
    ("bagholder trap", -0.45, "bagholder trap"),
    ("bull trap", -0.40, "bull trap"),
    ("downgrade to sell", -0.40, "downgrade to sell"),
    ("underperform", -0.30, "underperform"),
    ("margin call", -0.45, "margin call"),
)

# Bilingual negation / reversal cues (ResearchGate 379351322 / Loughran-McDonald NLP standard)
_NEGATION_PREFIX_RE = re.compile(
    r"(?:^|[\s,;:()\"'])"
    r"(?:không\s+hề|không\s+thể|không\s+đạt|không\s+bị|không\s+còn|không|"
    r"chưa\s+thể|chưa\s+đạt|chưa\s+có|chưa|chẳng|bác\s+bỏ|phủ\s+nhận|thất\s+bại\s+khi|"
    r"not|no\s+longer|never|failed\s+to|fails\s+to|unable\s+to|unlikely\s+to|"
    r"without|denied|denies|refuted|refutes)"
    r"(?:\s+\w+){0,2}\s*$",
    re.IGNORECASE,
)

# Finnhub Insider Sentiment (MSPR) & Form 4 / HOSE CBTT insider activity markers
_INSIDER_BUY_TERMS: tuple[str, ...] = (
    "lãnh đạo đăng ký mua",
    "chủ tịch đăng ký mua",
    "nội bộ mua vào",
    "cổ đông lớn mua vào",
    "mua vào cổ phiếu quỹ",
    "insider buying",
    "form 4 buy",
    "cluster insider buying",
    "ceo bought",
    "director bought",
)

_INSIDER_SELL_TERMS: tuple[str, ...] = (
    "lãnh đạo bán chui",
    "bán chui",
    "lãnh đạo đăng ký bán",
    "chủ tịch đăng ký bán",
    "cổ đông lớn thoái vốn",
    "đăng ký bán lớn",
    "insider dumping",
    "insider selling",
    "form 4 sale",
)

# Loughran-McDonald (2011) & Finnhub Filings Sentiment uncertainty / litigation / audit risk
_UNCERTAINTY_AND_LITIGATION_TERMS: tuple[str, ...] = (
    "thanh tra",
    "khởi tố",
    "bắt tạm giam",
    "kiểm toán ngoại trừ",
    "ý kiến ngoại trừ",
    "diện cảnh báo",
    "diện kiểm soát",
    "hủy niêm yết",
    "chậm công bố",
    "sec probe",
    "regulatory probe",
    "doj investigation",
    "going concern",
    "material weakness",
    "restatement",
    "class action lawsuit",
    "debt covenant breach",
    "subpoena",
)

# Investopedia & CNN Fear & Greed Pillar 3 (Momentum & Moving Averages)
_MOMENTUM_MA_TERMS: tuple[str, ...] = (
    "vượt đỉnh",
    "vượt ma200",
    "vượt ma50",
    "thủng ma200",
    "gãy ma200",
    "gãy nền",
    "bùng nổ theo đà",
    "golden cross",
    "death cross",
    "above 200-day",
    "below 200-day",
    "bullish breakout",
    "dead cat bounce",
    "bull trap",
)

# Vietstock Market Psychology & CNN Fear & Greed Pillar 4 (Breadth, 52W High/Low, Liquidity)
_BREADTH_LIQUIDITY_TERMS: tuple[str, ...] = (
    "thanh khoản lớn",
    "thanh khoản đột biến",
    "thanh khoản sụt giảm",
    "cạn cung",
    "tiền vào bắt đáy",
    "lan tỏa dòng tiền",
    "xanh vỏ đỏ lòng",
    "phân phối đỉnh",
    "trắng bên mua",
    "nằm sàn",
    "đỉnh 52 tuần",
    "đáy 52 tuần",
    "52-week high",
    "52-week low",
    "positive breadth",
    "bullish percent",
)

# CNN Fear & Greed Pillar 5 (Volatility, Options Put/Call & Margin Leverage Stress)
_VOLATILITY_LEVERAGE_TERMS: tuple[str, ...] = (
    "call margin",
    "giải chấp chéo",
    "bán giải chấp",
    "margin call",
    "short squeeze",
    "gamma squeeze",
    "high put/call",
    "vix spike",
    "multiple compression",
)

_BINARY_EVENT_KEYWORDS: tuple[str, ...] = (
    "báo cáo tài chính quý",
    "đại hội cổ đông",
    "đhđcđ",
    "nghị quyết hđqt",
    "nới room",
    "nâng hạng",
    "phán quyết",
    "earnings call",
    "earnings release",
    "fda approval",
    "fomc",
    "court verdict",
    "merger vote",
)


@dataclass(slots=True)
class EquityEntity:
    ticker: str
    resolved_name: str
    exchange: ExchangeCode
    is_vietnam: bool
    timestamp_utc: str


@dataclass(slots=True)
class OmniChannelQuery:
    layer: int  # 1..5
    lang: LangCode
    query: str
    category: str = "general"


@dataclass(slots=True)
class RawSentimentHit:
    url: str
    title: str
    snippet: str = ""
    content: str = ""
    published_at: datetime | None = None
    lang: LangCode | None = None
    # PASS-2 audit P2-8: True when the hit came from the caller-supplied
    # retrieved_documents bypass — such hits are capped at TIER_3 for
    # scoring so client content can never present as verified TIER_1/2.
    client_supplied: bool = False


@dataclass(slots=True)
class ScoredSentimentHit:
    url: str
    title: str
    text: str
    lang: LangCode
    domain: str
    platform: str
    ecosystem: Literal["official_and_media", "vn_social", "global_social"]
    tier: TierCode
    freshness: FreshnessBucket
    tier_weight: float
    time_decay: float
    effective_weight: float
    polarity_score: float  # -1.0 .. +1.0
    polarity: PolarityCode
    evidence_quote: str
    matched_tags: list[str] = field(default_factory=list)
    is_seeding: bool = False
    is_unverified_rumor: bool = False


def resolve_equity_entity(
    ticker: str,
    *,
    company_name: str | None = None,
    exchange: str | None = None,
    now: datetime | None = None,
) -> EquityEntity:
    """Resolve ticker against VN listed catalog (1,518 symbols) or global catalog."""
    clean_ticker = re.sub(r"[^A-Za-z0-9._-]", "", (ticker or "").strip()).upper()[:15]
    if not clean_ticker:
        raise ValueError("ticker must not be empty")

    ts = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    vn_catalog = load_vn_tickers()
    vn_rec = vn_catalog.get(clean_ticker)

    if vn_rec is not None:
        raw_ex = str(exchange or vn_rec.get("exchange") or "HOSE").upper().strip()
        if raw_ex in ("HSX", "HOSE"):
            norm_ex: ExchangeCode = "HOSE"
        elif raw_ex == "HNX":
            norm_ex = "HNX"
        elif raw_ex == "UPCOM":
            norm_ex = "UPCOM"
        else:
            norm_ex = "HOSE"
        name = (company_name or str(vn_rec.get("company_name") or "")).strip() or clean_ticker
        return EquityEntity(
            ticker=clean_ticker,
            resolved_name=name,
            exchange=norm_ex,
            is_vietnam=True,
            timestamp_utc=ts,
        )

    if clean_ticker in _GLOBAL_EQUITIES:
        default_name, default_ex = _GLOBAL_EQUITIES[clean_ticker]
        resolved_ex = _normalize_exchange(exchange, default=default_ex)
        return EquityEntity(
            ticker=clean_ticker,
            resolved_name=(company_name or default_name).strip(),
            exchange=resolved_ex,
            is_vietnam=False,
            timestamp_utc=ts,
        )

    resolved_ex = _normalize_exchange(exchange, default="OTHER")
    is_vn = resolved_ex in ("HOSE", "HNX", "UPCOM")
    return EquityEntity(
        ticker=clean_ticker,
        resolved_name=(company_name or clean_ticker).strip(),
        exchange=resolved_ex,
        is_vietnam=is_vn,
        timestamp_utc=ts,
    )


def _normalize_exchange(raw: str | None, *, default: ExchangeCode) -> ExchangeCode:
    if not raw:
        return default
    up = raw.strip().upper()
    if up in ("HOSE", "HSX"):
        return "HOSE"
    if up == "HNX":
        return "HNX"
    if up == "UPCOM":
        return "UPCOM"
    if up == "NASDAQ":
        return "NASDAQ"
    if up == "NYSE":
        return "NYSE"
    return "OTHER"


def build_omni_channel_queries(
    entity: EquityEntity,
    *,
    max_queries: int = 18,
) -> list[OmniChannelQuery]:
    """Phase 1: Generate parallel search queries across all 5 network layers."""
    t = entity.ticker
    name = entity.resolved_name
    name_clause = f'"{t}" OR "{name}"' if name and name.upper() != t else f'"{t}"'

    queries: list[OmniChannelQuery] = [
        # Layer 1: Official Exchanges, Regulators & Filings (Ground Truth)
        OmniChannelQuery(
            layer=1,
            lang="VI",
            query=f'site:hsx.vn OR site:hnx.vn OR site:ssc.gov.vn OR site:vsd.vn "{t}"',
            category="news",
        ),
        OmniChannelQuery(
            layer=1,
            lang="VI",
            query=f'"{t}" công bố thông tin nghị quyết HĐQT báo cáo tài chính giao dịch nội bộ',
            category="news",
        ),
        OmniChannelQuery(
            layer=1,
            lang="EN",
            query=(
                f'site:sec.gov OR site:finance.yahoo.com "{t}" '
                f"earnings filing guidance transcript insider"
            ),
            category="news",
        ),
        # Layer 2: Institutional Research & Financial Media Network
        OmniChannelQuery(
            layer=2,
            lang="VI",
            query=(
                f"site:cafef.vn OR site:vietstock.vn OR site:finance.vietstock.vn "
                f'OR site:vneconomy.vn OR site:tinnhanhchungkhoan.vn "{t}"'
            ),
            category="news",
        ),
        OmniChannelQuery(
            layer=2,
            lang="VI",
            query=(
                f"site:ssi.com.vn OR site:vndirect.com.vn OR site:hsc.com.vn "
                f'OR site:vietcap.com.vn "{t} báo cáo phân tích"'
            ),
            category="general",
        ),
        OmniChannelQuery(
            layer=2,
            lang="EN",
            query=(
                f"site:bloomberg.com OR site:reuters.com OR site:wsj.com "
                f"OR site:ft.com OR site:seekingalpha.com ({name_clause})"
            ),
            category="news",
        ),
        # Layer 3: Vietnamese Social Media & Investor Communities
        OmniChannelQuery(
            layer=3,
            lang="VI",
            query=f'site:facebook.com "{t} cổ phiếu" OR "{t} nhận định" OR "{t} phím hàng"',
            category="general",
        ),
        OmniChannelQuery(
            layer=3,
            lang="VI",
            query=f'site:fireant.vn OR site:f319.com OR site:voz.vn "{t}"',
            category="general",
        ),
        OmniChannelQuery(
            layer=3,
            lang="VI",
            query=f'site:t.me "{t} cổ phiếu" OR "{t} mua bán"',
            category="general",
        ),
        OmniChannelQuery(
            layer=3,
            lang="VI",
            query=f'site:threads.net "{t} cổ phiếu"',
            category="general",
        ),
        # Layer 4: Global Social Media & Real-Time Cashtag Streams
        OmniChannelQuery(
            layer=4,
            lang="EN",
            query=f'site:x.com "${t}" OR "#{t}" stock sentiment',
            category="general",
        ),
        OmniChannelQuery(
            layer=4,
            lang="EN",
            query=(
                f"site:reddit.com/r/wallstreetbets OR site:reddit.com/r/stocks "
                f'OR site:reddit.com/r/investing OR site:reddit.com/r/VietNam "{t}"'
            ),
            category="general",
        ),
        OmniChannelQuery(
            layer=4,
            lang="EN",
            query=f'site:stocktwits.com OR site:substack.com "${t}"',
            category="general",
        ),
        # Layer 5: Video, Livestream & KOL Commentary (YouTube / TikTok)
        OmniChannelQuery(
            layer=5,
            lang="VI",
            query=f'site:youtube.com OR site:tiktok.com "{t} cổ phiếu nhận định phân tích"',
            category="general",
        ),
        OmniChannelQuery(
            layer=5,
            lang="EN",
            query=f'site:youtube.com OR site:tiktok.com "${t}" stock analysis news',
            category="general",
        ),
        # Extended Institutional Quant, Options, Dark Pool & Survey Portals
        OmniChannelQuery(
            layer=2,
            lang="VI",
            query=(
                f"site:fiintrade.vn OR site:wichart.vn OR site:simplize.vn "
                f'OR site:24hmoney.vn "{t}" khối ngoại tự doanh margin định giá'
            ),
            category="general",
        ),
        OmniChannelQuery(
            layer=1,
            lang="EN",
            query=(
                f"site:cboe.com OR site:finra.org OR site:aaii.com "
                f'OR site:naaim.org "{t}" put call short volume sentiment'
            ),
            category="general",
        ),
        OmniChannelQuery(
            layer=2,
            lang="EN",
            query=(
                f"site:finviz.com OR site:barchart.com OR site:tipranks.com "
                f'OR site:whalewisdom.com OR site:quiverquant.com "{t}" insider 13F options'
            ),
            category="general",
        ),
        OmniChannelQuery(
            layer=4,
            lang="EN",
            query=(
                f"site:unusualwhales.com OR site:swaggystocks.com OR site:apewisdom.io "
                f'OR site:sentimentrader.com "${t}" options flow dark pool'
            ),
            category="general",
        ),
    ]

    if entity.is_vietnam:
        # Prioritize Vietnamese official, media, and VN social layers first while
        # preserving global cashtag/institutional coverage.
        ordered = [q for q in queries if q.lang == "VI"] + [q for q in queries if q.lang == "EN"]
    else:
        ordered = [q for q in queries if q.lang == "EN"] + [q for q in queries if q.lang == "VI"]

    return ordered[: max(1, min(max_queries, len(ordered)))]


def _extract_domain(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _detect_language(text: str, fallback: LangCode | None = None) -> LangCode:
    if fallback in ("VI", "EN"):
        return fallback
    # Vietnamese diacritic range check
    if re.search(
        r"[àáảãạăắằẳẵặâấầẩẫậèéẻẽẹêếềểễệìíỉĩịòóỏõọôốồổỗộơớờởỡợùúủũụưứừửữựỳýỷỹỵđ]", text.lower()
    ):
        return "VI"
    return "EN"


def is_entity_collision(entity: EquityEntity, hit: RawSentimentHit) -> bool:
    """Phase 2A: Strict Entity Resolution (Anti-Collision).

    Returns True when ``hit`` refers to a non-target entity sharing the same ticker.
    """
    combined = f"{hit.title} {hit.snippet} {hit.content}".lower()
    ticker_lower = entity.ticker.lower()

    # Ensure the document actually mentions the ticker or resolved company name
    name_lower = entity.resolved_name.lower()
    ticker_pattern = rf"(?<![a-z0-9]){re.escape(ticker_lower)}(?![a-z0-9])"
    has_ticker = bool(re.search(ticker_pattern, combined))
    has_name = bool(name_lower and len(name_lower) > 3 and name_lower in combined)
    if not has_ticker and not has_name:
        return True

    rules = _TICKER_COLLISION_RULES.get(entity.ticker)
    if rules is not None:
        bad_terms, anchor_terms = rules
        has_bad = any(b in combined for b in bad_terms)
        has_anchor = any(a in combined for a in anchor_terms)
        if has_bad and not has_anchor:
            return True

    return False


def _classify_domain_and_tier(
    domain: str, text: str, lang: LangCode
) -> tuple[TierCode, str, Literal["official_and_media", "vn_social", "global_social"]]:
    """Map domain + content depth to (TierCode, display_platform, ecosystem)."""
    for d in _TIER_1_DOMAINS:
        if domain == d or domain.endswith("." + d):
            return "TIER_1", d, "official_and_media"

    for d in _TIER_2_DOMAINS:
        if domain == d or domain.endswith("." + d):
            return "TIER_2", d, "official_and_media"

    for host, label in _VN_SOCIAL_PLATFORM_MAP.items():
        if domain == host or domain.endswith("." + host):
            tier: TierCode = "TIER_3" if _is_high_signal_social(text, domain) else "TIER_4"
            return tier, label, "vn_social"

    for host, label in _GLOBAL_SOCIAL_PLATFORM_MAP.items():
        if domain == host or domain.endswith("." + host):
            tier = "TIER_3" if _is_high_signal_social(text, domain) else "TIER_4"
            return tier, label, "global_social"

    if domain == "youtube.com" or domain.endswith(".youtube.com") or domain == "youtu.be":
        eco: Literal["official_and_media", "vn_social", "global_social"] = (
            "vn_social" if lang == "VI" else "global_social"
        )
        tier = "TIER_3" if len(text) >= 140 else "TIER_4"
        return tier, "YouTube", eco

    # PASS-2 audit P1-2: the old keyword-substring fallback granted TIER_2
    # (weight 0.75, "official_and_media") to ANY domain merely containing
    # "market"/"news"/... — an attacker-chosen domain could self-promote.
    # Unlisted domains now fall through to the social/eco classification
    # (TIER_3/TIER_4). Registered press lives in _TIER_2_DOMAINS above.
    eco = "vn_social" if lang == "VI" else "global_social"
    tier = "TIER_3" if _is_high_signal_social(text, domain) else "TIER_4"
    return tier, domain or ("VN-Web" if lang == "VI" else "Global-Web"), eco


def _is_high_signal_social(text: str, domain: str) -> bool:
    """Distinguish TIER_3 fundamental threads/KOL analysis from TIER_4 raw chatter."""
    if len(text.strip()) < 140:
        return False
    low = text.lower()
    analytical_markers = (
        "p/e",
        "p/b",
        "roe",
        "roa",
        "eps",
        "ebitda",
        "biên lợi nhuận",
        "doanh thu",
        "lợi nhuận sau thuế",
        "báo cáo tài chính",
        "dòng tiền",
        "định giá",
        "valuation",
        "free cash flow",
        "gross margin",
        "operating margin",
        "balance sheet",
        "guidance",
    )
    hits = sum(1 for m in analytical_markers if m in low)
    if hits >= 2:
        return True
    return any(d in domain for d in ("substack.com", "fireant.vn", "reddit.com")) and hits >= 1


def _compute_freshness(
    published_at: datetime | None, *, now: datetime
) -> tuple[FreshnessBucket, float]:
    """Phase 2C Time_Decay schedule."""
    if published_at is None:
        return "STALE", TIME_DECAY_WEIGHTS["STALE"]
    pub = published_at if published_at.tzinfo is not None else published_at.replace(tzinfo=UTC)
    age_hours = (now - pub.astimezone(UTC)).total_seconds() / 3600.0
    if age_hours < 0.0:
        # PASS-2 audit P3-15: future-dated content must not earn BREAKING_6H
        # (the old max(0, ...) clamp gave it maximum freshness) — stale.
        return "STALE", TIME_DECAY_WEIGHTS["STALE"]
    age_hours = max(0.0, age_hours)
    if age_hours <= 6.0:
        return "BREAKING_6H", TIME_DECAY_WEIGHTS["BREAKING_6H"]
    if age_hours <= 24.0:
        return "INTRADAY_24H", TIME_DECAY_WEIGHTS["INTRADAY_24H"]
    if age_hours <= 72.0:
        return "SHORT_3D", TIME_DECAY_WEIGHTS["SHORT_3D"]
    if age_hours <= 168.0:
        return "WEEKLY_7D", TIME_DECAY_WEIGHTS["WEEKLY_7D"]
    return "STALE", TIME_DECAY_WEIGHTS["STALE"]


def _phrasing_fingerprint(text: str) -> str:
    """Normalized text fingerprint for copy-paste seeding detection across platforms."""
    cleaned = re.sub(r"https?://\S+", "", text.lower())
    cleaned = re.sub(r"[^\w\sà-ỹ]", " ", cleaned)
    tokens = [t for t in cleaned.split() if len(t) > 2]
    core = " ".join(tokens[:24])
    return hashlib.sha256(core.encode("utf-8")).hexdigest()[:16]


def _is_negated_match(low_text: str, phrase: str) -> bool:
    """True when EVERY occurrence is negated (kept for callers that need a
    single boolean); scoring itself now walks occurrences via
    ``_iter_occurrence_negated`` — an early un-negated mention no longer
    masks a later negated one (docs/29-sentiment audit P1-1)."""
    idx = low_text.find(phrase)
    if idx < 0:
        return False
    occurrences = 0
    negated = 0
    for _idx, neg in _iter_occurrence_negated(low_text, phrase):
        occurrences += 1
        negated += 1 if neg else 0
    return occurrences > 0 and negated == occurrences


def _iter_occurrence_negated(low_text: str, phrase: str):
    """Yield (index, is_negated) per occurrence of ``phrase``."""
    idx = low_text.find(phrase)
    while idx >= 0:
        yield idx, _occurrence_negated(low_text, idx)
        idx = low_text.find(phrase, idx + len(phrase))


def _occurrence_negated(low_text: str, idx: int) -> bool:
    if idx <= 0:
        return False
    window = low_text[max(0, idx - 42) : idx]
    # Stop at the nearest sentence/clause boundary inside the lookbehind window
    for sep in (".", "!", "?", ";", "\n"):
        pos = window.rfind(sep)
        if pos != -1:
            window = window[pos + 1 :]
    return bool(_NEGATION_PREFIX_RE.search(window))


def _extract_lexicon_terms(text: str) -> list[str]:
    """Extract matched lexicon phrases for Loughran-McDonald corpus IDF weighting."""
    low = text.lower()
    terms: list[str] = []
    for lex in (_VI_BULLISH_LEXICON, _VI_BEARISH_LEXICON, _EN_BULLISH_LEXICON, _EN_BEARISH_LEXICON):
        for phrase, _weight, _tag in lex:
            if phrase in low:
                terms.append(phrase)
    return terms


def _score_lexicon(
    text: str,
    lang: LangCode,
    *,
    idf_weights: dict[str, float] | None = None,
) -> tuple[float, PolarityCode, list[str]]:
    """Phase 3: Score bilingual financial lexicon with negation guard & optional TF-IDF."""
    low = text.lower()
    score = 0.0
    tags: list[str] = []

    for lex in (_VI_BULLISH_LEXICON, _VI_BEARISH_LEXICON, _EN_BULLISH_LEXICON, _EN_BEARISH_LEXICON):
        for phrase, weight, tag in lex:
            if phrase in low:
                # PASS-2 audit P2-6: longest-match-wins — "tự doanh gom hàng"
                # must not also score the nested "gom hàng".
                all_lexicons = (
                    _VI_BULLISH_LEXICON,
                    _VI_BEARISH_LEXICON,
                    _EN_BULLISH_LEXICON,
                    _EN_BEARISH_LEXICON,
                )
                if any(
                    len(other) > len(phrase) and phrase in other and other in low
                    for lex2 in all_lexicons
                    for other, _w2, _t2 in lex2
                ):
                    continue
                mult = idf_weights.get(phrase, 1.0) if idf_weights else 1.0
                # PASS-2 audit P1-1: score EACH occurrence — an early
                # un-negated mention no longer masks a negated one.
                neg_seen = False
                pos_seen = False
                for _idx, occ_neg in _iter_occurrence_negated(low, phrase):
                    if occ_neg:
                        score += -0.75 * weight * mult
                        neg_seen = True
                    else:
                        score += weight * mult
                        pos_seen = True
                if neg_seen:
                    neg_tag = f"phủ định: {tag}" if lang == "VI" else f"negated: {tag}"
                    if neg_tag not in tags:
                        tags.append(neg_tag)
                if pos_seen and not neg_seen and tag not in tags:
                    tags.append(tag)

    # Secondary general financial polarity terms when no specific slang phrase fired
    if score == 0.0:
        pos_fallback = (
            ("tăng trưởng", 0.20),
            ("tích cực", 0.20),
            ("khởi sắc", 0.20),
            ("mua ròng", 0.25),
            ("lãi lớn", 0.25),
            ("bullish", 0.25),
            ("rally", 0.20),
            ("upside", 0.20),
            ("growth", 0.15),
        )
        neg_fallback = (
            ("bán ròng", -0.25),
            ("lao dốc", -0.30),
            ("tiêu cực", -0.20),
            ("rủi ro", -0.15),
            ("giảm sàn", -0.35),
            ("bearish", -0.25),
            ("selloff", -0.25),
            ("downside", -0.20),
            ("plunge", -0.30),
        )
        for w_phrase, w_val in pos_fallback:
            if w_phrase in low:
                score += -0.75 * w_val if _is_negated_match(low, w_phrase) else w_val
        for w_phrase, w_val in neg_fallback:
            if w_phrase in low:
                score += -0.75 * w_val if _is_negated_match(low, w_phrase) else w_val

    clamped = max(-1.0, min(1.0, round(score, 4)))
    if clamped >= 0.12:
        polarity: PolarityCode = "POSITIVE"
    elif clamped <= -0.12:
        polarity = "NEGATIVE"
    else:
        polarity = "NEUTRAL"
    return clamped, polarity, tags


def _extract_verbatim_quote(hit: RawSentimentHit, ticker: str) -> str:
    """Extract a short verbatim quote from the hit's original text (never fabricated)."""
    source_body = (hit.snippet or hit.content or hit.title or "").strip()
    if not source_body:
        return hit.title.strip()[:220]
    sentences = [s.strip() for s in re.split(r"(?<=[.!?。\n])\s+", source_body) if s.strip()]
    t_low = ticker.lower()
    for s in sentences:
        if t_low in s.lower() and 15 <= len(s) <= 260:
            return s
    for s in sentences:
        if 15 <= len(s) <= 260:
            return s
    return source_body[:240].strip()


def analyze_omni_channel_sentiment(
    entity: EquityEntity,
    hits: list[RawSentimentHit],
    *,
    now: datetime | None = None,
    ohlcv_bars: list[OhlcvBarInput] | None = None,
    ohlcv_source_name: str | None = None,
    ohlcv_source_url: str | None = None,
    ohlcv_as_of: str | None = None,
    market_proxies: MarketProxiesInput | None = None,
    fundamentals: FundamentalSnapshotInput | None = None,
) -> dict[str, Any]:
    """Execute Phase 2 & Phase 3 over raw hits + real market telemetry."""
    ref_now = (now or datetime.now(UTC)).astimezone(UTC)

    ohlcv_metrics = compute_ohlcv_sentiment_metrics(
        ohlcv_bars,
        source_name=ohlcv_source_name,
        source_url=ohlcv_source_url,
        as_of=ohlcv_as_of,
    )
    proxy_metrics = compute_market_proxy_metrics(market_proxies)
    fundamentals_dict: dict[str, Any] | None = None
    if fundamentals is not None:
        fundamentals_dict = {
            k: v
            for k, v in {
                "pe": fundamentals.pe,
                "pb": fundamentals.pb,
                "roe": fundamentals.roe,
                "revenue_growth_yoy": fundamentals.revenue_growth_yoy,
                "profit_growth_yoy": fundamentals.profit_growth_yoy,
                "debt_to_equity": fundamentals.debt_to_equity,
                "market_cap": fundamentals.market_cap,
                "source": fundamentals.source,
                "source_url": fundamentals.source_url,
                "as_of": fundamentals.as_of,
            }.items()
            if v is not None
        } or None

    # Phase 2A: Strict Entity Resolution (drop cross-market ticker collisions)
    valid_hits = [h for h in hits if not is_entity_collision(entity, h)]

    if not valid_hits:
        if ohlcv_metrics.get("available") or proxy_metrics.get("available") or fundamentals_dict:
            return _quant_only_payload(
                entity,
                ohlcv_metrics=ohlcv_metrics,
                proxy_metrics=proxy_metrics,
                fundamentals=fundamentals_dict,
            )
        return _insufficient_data_payload(entity)

    # First pass: classify domains/tiers so we know whether Layer 1/2 corroborates claims
    prelim: list[
        tuple[
            RawSentimentHit,
            str,
            LangCode,
            TierCode,
            str,
            Literal["official_and_media", "vn_social", "global_social"],
        ]
    ] = []
    tier12_corpus_parts: list[str] = []
    for h in valid_hits:
        full_text = " ".join(p for p in (h.title, h.snippet, h.content) if p).strip()
        lang = _detect_language(full_text, h.lang)
        dom = _extract_domain(h.url)
        tier, platform, eco = _classify_domain_and_tier(dom, full_text, lang)
        if h.client_supplied and tier in ("TIER_1", "TIER_2"):
            # PASS-2 audit P2-8: client-supplied documents must never present
            # as verified TIER_1/2 evidence — cap at TIER_3 so a caller cannot
            # forge "SEC-grade" provenance with zero corroboration.
            tier = "TIER_3"
        prelim.append((h, full_text, lang, tier, platform, eco))
        if tier in ("TIER_1", "TIER_2"):
            tier12_corpus_parts.append(full_text.lower())

    tier12_corpus = " ".join(tier12_corpus_parts)

    # Loughran-McDonald corpus IDF map across prelim hits (>= 3 hits)
    doc_term_lists = [_extract_lexicon_terms(full_text) for _h, full_text, *_rest in prelim]
    idf_weights = compute_tfidf_idf_map(doc_term_lists)

    # Phase 2B: Social Manipulation & Seeding Detector + Unverified Rumor Discounting
    fingerprint_counts: dict[str, int] = {}
    for _h, full_text, _lang, tier, _plat, _eco in prelim:
        if tier in ("TIER_3", "TIER_4"):
            fp = _phrasing_fingerprint(full_text)
            fingerprint_counts[fp] = fingerprint_counts.get(fp, 0) + 1

    seen_seeding_fps: set[str] = set()
    seeding_collapsed_count = 0
    unverified_rumor_count = 0
    scored_hits: list[ScoredSentimentHit] = []
    rumor_emotion_scores: list[float] = []

    for h, full_text, lang, tier, platform, eco in prelim:
        low_text = full_text.lower()
        is_social = tier in ("TIER_3", "TIER_4")
        fp = _phrasing_fingerprint(full_text) if is_social else ""
        has_seeding_phrase = is_social and any(p in low_text for p in _SEEDING_PATTERNS)
        is_dup_seeding = is_social and fingerprint_counts.get(fp, 0) >= 2

        if has_seeding_phrase or is_dup_seeding:
            if fp in seen_seeding_fps or ("__promo__" in seen_seeding_fps and has_seeding_phrase):
                seeding_collapsed_count += 1
                continue
            if fp:
                seen_seeding_fps.add(fp)
            if has_seeding_phrase:
                seen_seeding_fps.add("__promo__")
            seeding_collapsed_count += 1
            # Collapse into 1 single low-weight TIER_4 signal
            tier = "TIER_4"
            is_seeding = True
        else:
            is_seeding = False

        # Check unverified social rumor
        has_rumor_marker = is_social and any(r in low_text for r in _RUMOR_PATTERNS)
        is_unverified_rumor = False
        if has_rumor_marker:
            # Check whether any key rumor token is corroborated in Tier 1/2
            corroborated = any(
                kw in tier12_corpus
                for kw in (
                    "khởi tố",
                    "bắt",
                    "thanh tra",
                    "chia thưởng",
                    "thâu tóm",
                    "probe",
                    "investigation",
                )
                if kw in low_text
            )
            if not corroborated:
                is_unverified_rumor = True
                unverified_rumor_count += 1

        freshness, time_decay = _compute_freshness(h.published_at, now=ref_now)
        tier_weight = TIER_WEIGHTS[tier]
        effective_weight = round(tier_weight * time_decay, 4)
        pol_score, pol_code, matched_tags = _score_lexicon(full_text, lang, idf_weights=idf_weights)
        quote = _extract_verbatim_quote(h, entity.ticker)

        if is_unverified_rumor:
            rumor_emotion_scores.append(pol_score)

        scored_hits.append(
            ScoredSentimentHit(
                url=h.url,
                title=h.title.strip() or quote[:120],
                text=full_text,
                lang=lang,
                domain=_extract_domain(h.url) or platform.lower(),
                platform=platform,
                ecosystem=eco,
                tier=tier,
                freshness=freshness,
                tier_weight=tier_weight,
                time_decay=time_decay,
                effective_weight=effective_weight,
                polarity_score=pol_score,
                polarity=pol_code,
                evidence_quote=quote,
                matched_tags=matched_tags,
                is_seeding=is_seeding,
                is_unverified_rumor=is_unverified_rumor,
            )
        )

    # Factual hits exclude unverified social rumors (Phase 2B-2 rule)
    factual_hits = [s for s in scored_hits if not s.is_unverified_rumor]
    # PASS-2 audit P2-9: the old `and not scored_hits` made this branch dead
    # (scored_hits is provably non-empty here) — rumor-only input now returns
    # the honest INSUFFICIENT-factual payload instead of falling through.
    if not factual_hits:
        if ohlcv_metrics.get("available") or proxy_metrics.get("available") or fundamentals_dict:
            return _quant_only_payload(
                entity,
                ohlcv_metrics=ohlcv_metrics,
                proxy_metrics=proxy_metrics,
                fundamentals=fundamentals_dict,
                seeding_collapsed_count=seeding_collapsed_count,
                unverified_rumor_count=unverified_rumor_count,
                all_hits=scored_hits,
            )
        return _insufficient_data_payload(entity)

    def _weighted_avg(items: list[ScoredSentimentHit]) -> float:
        total_w = sum(i.effective_weight for i in items)
        if total_w <= 0.0:
            return 0.0
        return round(
            max(
                -1.0, min(1.0, sum(i.polarity_score * i.effective_weight for i in items) / total_w)
            ),
            4,
        )

    official_hits = [s for s in factual_hits if s.ecosystem == "official_and_media"]
    vn_social_factual = [s for s in factual_hits if s.ecosystem == "vn_social"]
    global_social_factual = [s for s in factual_hits if s.ecosystem == "global_social"]

    vn_social_all = [s for s in scored_hits if s.ecosystem == "vn_social"]
    global_social_all = [s for s in scored_hits if s.ecosystem == "global_social"]
    social_all = vn_social_all + global_social_all

    composite_score = _weighted_avg(factual_hits)
    official_score = _weighted_avg(official_hits)
    vn_social_score = _weighted_avg(vn_social_all)
    global_social_score = _weighted_avg(global_social_all)
    social_crowd_score = _weighted_avg(social_all)

    # Institutional (Tier 1/2) vs Retail Social (Tier 3/4) spread (-2.00 to +2.00)
    if official_hits and social_all:
        spread = round(max(-2.0, min(2.0, official_score - social_crowd_score)), 4)
    else:
        spread = 0.0

    label = _score_to_label(composite_score)
    momentum_24h = _compute_momentum_24h(factual_hits)

    # Confidence scales with total effective weight and tier coverage
    total_eff_weight = sum(s.effective_weight for s in factual_hits)
    has_tier12 = any(s.tier in ("TIER_1", "TIER_2") for s in factual_hits)
    raw_conf = min(0.95, 0.22 + 0.18 * min(total_eff_weight, 3.5) + (0.12 if has_tier12 else 0.0))
    confidence = 0.15 if not factual_hits else round(raw_conf, 2)

    # Risk circuit breakers & Market Regime (Phase 3)
    breakers: list[str] = []
    if seeding_collapsed_count > 0:
        breakers.append("COORDINATED_SOCIAL_SEEDING")
    if unverified_rumor_count > 0:
        breakers.append("UNVERIFIED_RUMOR_SPIKE")

    regime = _classify_regime(
        official_score=official_score,
        social_score=social_crowd_score,
        official_hits=official_hits,
        social_hits=social_all,
        all_hits=scored_hits,
        total_eff_weight=total_eff_weight,
    )

    if (
        regime == "EUPHORIC_DISTRIBUTION_TRAP"
        or (social_crowd_score >= 0.50 and official_score <= -0.25)
    ) and "RETAIL_FOMO_VS_FOREIGN_SELLING" not in breakers:
        breakers.append("RETAIL_FOMO_VS_FOREIGN_SELLING")

    if any(
        any(
            k in s.text.lower()
            for k in ("call margin", "giải chấp chéo", "bán giải chấp", "margin call")
        )
        for s in scored_hits
    ):
        breakers.append("MARGIN_CALL_CASCADE_RISK")

    if total_eff_weight < 0.15:
        regime = "LOW_SIGNAL_NOISE"
        confidence = min(confidence, 0.18)
        if "INSUFFICIENT_DATA" not in breakers:
            breakers.append("INSUFFICIENT_DATA")

    # Top verified drivers (exclude unverified rumors, sorted by effective_weight * |polarity|)
    sorted_drivers = sorted(
        factual_hits,
        key=lambda x: (x.effective_weight * (0.5 + abs(x.polarity_score)), x.effective_weight),
        reverse=True,
    )[:8]

    top_drivers = [
        {
            "rank": idx,
            "headline_or_post_summary": d.title[:240],
            "lang": d.lang,
            "platform_or_domain": d.domain,
            "tier": d.tier,
            "freshness": d.freshness,
            "effective_weight": d.effective_weight,
            "polarity": d.polarity,
            "evidence_quote": d.evidence_quote,
            "url": _safe_http_url(d.url),
        }
        for idx, d in enumerate(sorted_drivers, start=1)
    ]

    key_catalysts = _collect_unique_tags(official_hits, limit=6)
    if not key_catalysts and official_hits:
        key_catalysts = [h.title[:140] for h in official_hits[:3]]

    vn_platforms = sorted({s.platform for s in vn_social_all})
    global_platforms = sorted({s.platform for s in global_social_all})
    trending_slang_vi = _collect_unique_tags(vn_social_all, limit=6)
    trending_tags_en = _collect_unique_tags(global_social_all, limit=6)
    if entity.ticker and f"${entity.ticker}" not in trending_tags_en and global_social_all:
        trending_tags_en.insert(0, f"${entity.ticker}")

    crowd_narrative_vi = _build_vn_narrative(
        entity.ticker,
        vn_social_all,
        vn_social_score,
        seeding_collapsed_count,
        unverified_rumor_count,
    )
    crowd_narrative_en = _build_en_narrative(entity.ticker, global_social_all, global_social_score)

    if seeding_collapsed_count > 0 or unverified_rumor_count > 0:
        audit_notes = (
            f"Detected {seeding_collapsed_count} copy-paste/VIP seeding post(s) "
            f"and {unverified_rumor_count} uncorroborated social rumor(s); "
            f"discounted from factual composite score."
        )
    else:
        social_factual_count = len(vn_social_factual) + len(global_social_factual)
        audit_notes = (
            f"Verified {len(factual_hits)} cross-network signal(s) across "
            f"{len(official_hits)} official/media and {social_factual_count} "
            f"social sources; no coordinated seeding detected."
        )

    inst_weighted = [(sh.effective_weight, sh.polarity_score) for sh in official_hits]
    all_weighted = [(sh.effective_weight, sh.polarity_score) for sh in factual_hits]
    nlp_telemetry: dict[str, Any] = {
        "tfidf_corpus_weighting": bool(idf_weights),
        "corpus_doc_count": len(prelim),
        "tetlock_media_pessimism": compute_tetlock_pessimism_ratio(inst_weighted),
        "consensus_polarization": compute_consensus_polarization(all_weighted),
        "source_diversity_entropy": compute_source_diversity_entropy(
            [sh.domain or sh.platform for sh in factual_hits]
        ),
    }

    benchmark_standards = _build_benchmark_standards(
        factual_hits=factual_hits,
        official_hits=official_hits,
        social_hits=social_all,
        all_hits=scored_hits,
        official_score=official_score,
        social_score=social_crowd_score,
        seeding_collapsed_count=seeding_collapsed_count,
        unverified_rumor_count=unverified_rumor_count,
        ohlcv_metrics=ohlcv_metrics,
        proxy_metrics=proxy_metrics,
        fundamentals=fundamentals_dict,
        nlp_telemetry=nlp_telemetry,
    )

    return {
        "module": "omni_channel_equity_sentiment_v2",
        "entity": {
            "ticker": entity.ticker,
            "resolved_name": entity.resolved_name,
            "exchange": entity.exchange,
            "timestamp_utc": entity.timestamp_utc,
        },
        "sentiment_summary": {
            "composite_score": composite_score,
            "institutional_vs_retail_spread": spread,
            "label": label,
            "confidence": confidence,
            "market_regime": regime,
            "momentum_24h": momentum_24h,
        },
        "benchmark_standards": benchmark_standards,
        "network_telemetry": {
            "official_and_media": {
                "weighted_score": official_score,
                "hit_count": len(official_hits),
                "key_catalysts": key_catalysts,
            },
            "vn_social_ecosystem": {
                "weighted_score": vn_social_score,
                "platforms_covered": vn_platforms,
                "hit_count": len(vn_social_all),
                "crowd_narrative_vi": crowd_narrative_vi,
                "trending_slang_vi": trending_slang_vi,
            },
            "global_social_ecosystem": {
                "weighted_score": global_social_score,
                "platforms_covered": global_platforms,
                "hit_count": len(global_social_all),
                "crowd_narrative_en": crowd_narrative_en,
                "trending_tags_en": trending_tags_en,
            },
        },
        "social_integrity_audit": {
            "manipulation_or_seeding_detected": seeding_collapsed_count > 0,
            "unverified_rumor_count": unverified_rumor_count,
            "audit_notes": audit_notes,
        },
        "top_verified_drivers": top_drivers,
        "risk_circuit_breakers": breakers,
    }


def _safe_http_url(url: str | None) -> str | None:
    """Allow only http(s) driver URLs with a non-empty host (block javascript:/data:)."""
    if not url:
        return None
    cleaned = url.strip()
    if not cleaned:
        return None
    try:
        parsed = urlsplit(cleaned)
    except ValueError:
        return None
    if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
        return cleaned
    return None


def _score_to_label(score: float) -> SentimentLabel:
    if score >= 0.45:
        return "STRONG_BULLISH"
    if score >= 0.15:
        return "BULLISH"
    if score <= -0.45:
        return "STRONG_BEARISH"
    if score <= -0.15:
        return "BEARISH"
    return "NEUTRAL"


def _compute_momentum_24h(hits: list[ScoredSentimentHit]) -> Momentum24h:
    recent = [h for h in hits if h.freshness in ("BREAKING_6H", "INTRADAY_24H")]
    older = [h for h in hits if h.freshness in ("SHORT_3D", "WEEKLY_7D", "STALE")]
    if not recent:
        return "UNKNOWN"
    recent_avg = sum(h.polarity_score for h in recent) / len(recent)
    if not older:
        if recent_avg >= 0.20:
            return "IMPROVING"
        if recent_avg <= -0.20:
            return "DETERIORATING"
        return "STABLE"
    older_avg = sum(h.polarity_score for h in older) / len(older)
    delta = recent_avg - older_avg
    if delta >= 0.15:
        return "IMPROVING"
    if delta <= -0.15:
        return "DETERIORATING"
    return "STABLE"


def _classify_regime(
    *,
    official_score: float,
    social_score: float,
    official_hits: list[ScoredSentimentHit],
    social_hits: list[ScoredSentimentHit],
    all_hits: list[ScoredSentimentHit],
    total_eff_weight: float,
) -> MarketRegime:
    if total_eff_weight < 0.15 or not all_hits:
        return "LOW_SIGNAL_NOISE"

    # EUPHORIC_DISTRIBUTION_TRAP: Extreme social euphoria (> +0.75) vs Tier 1/2 negative (< -0.30)
    if social_hits and social_score > 0.75 and official_hits and official_score < -0.30:
        return "EUPHORIC_DISTRIBUTION_TRAP"

    # PANIC_CAPITULATION: Extreme social panic (< -0.80) despite intact Tier 1/2 fundamentals
    if social_hits and social_score < -0.80 and official_score >= -0.15:
        return "PANIC_CAPITULATION"

    # STEALTH_ACCUMULATION: Tier 1/2 positive/neutral + inst buying while social is quiet/skeptical
    has_inst_buying = any(
        any(
            k in h.text.lower()
            for k in (
                "khối ngoại mua ròng",
                "tự doanh gom hàng",
                "tự doanh mua ròng",
                "institutional accumulation",
                "buyback",
            )
        )
        for h in official_hits
    )
    if (
        official_hits
        and official_score >= 0.0
        and has_inst_buying
        and (not social_hits or social_score <= 0.15)
    ):
        return "STEALTH_ACCUMULATION"

    # MOMENTUM_MARKUP: Strong aligned bullishness across official/brokers and social media
    if official_score >= 0.25 and (not social_hits or social_score >= 0.20):
        return "MOMENTUM_MARKUP"

    # EVENT_DRIVEN_BINARY: Pending major catalyst or sharp polarization
    has_binary_catalyst = any(
        any(k in h.text.lower() for k in _BINARY_EVENT_KEYWORDS) for h in all_hits
    )
    has_pos = any(h.polarity == "POSITIVE" for h in all_hits)
    has_neg = any(h.polarity == "NEGATIVE" for h in all_hits)
    if has_binary_catalyst or (has_pos and has_neg):
        return "EVENT_DRIVEN_BINARY"

    if official_score >= 0.20 or social_score >= 0.30:
        return "MOMENTUM_MARKUP"
    return "EVENT_DRIVEN_BINARY"


def _collect_unique_tags(hits: list[ScoredSentimentHit], *, limit: int = 6) -> list[str]:
    out: list[str] = []
    for h in hits:
        for tag in h.matched_tags:
            if tag not in out:
                out.append(tag)
                if len(out) >= limit:
                    return out
    return out


def _build_vn_narrative(
    ticker: str,
    vn_hits: list[ScoredSentimentHit],
    score: float,
    seeding_count: int,
    rumor_count: int,
) -> str:
    if not vn_hits:
        return f"Chưa ghi nhận thảo luận đáng kể trên mạng xã hội Việt Nam về {ticker}."
    platforms = ", ".join(sorted({h.platform for h in vn_hits}))
    mood = (
        "tích cực/hưng phấn"
        if score >= 0.20
        else ("bi quan/thận trọng" if score <= -0.20 else "phân hóa/trung lập")
    )
    base = (
        f"Tâm lý nhà đầu tư cá nhân và KOL trên {platforms} đối với {ticker} "
        f"đang ở trạng thái {mood} (điểm {score:+.2f} trên {len(vn_hits)} tín hiệu)."
    )
    if seeding_count > 0 or rumor_count > 0:
        return (
            f"{base} Hệ thống phát hiện {seeding_count} bài đăng trùng lặp/phím hàng VIP "
            f"và {rumor_count} tin đồn chưa kiểm chứng, đã chiết khấu khỏi điểm số cơ bản."
        )
    return base


def _build_en_narrative(
    ticker: str,
    en_hits: list[ScoredSentimentHit],
    score: float,
) -> str:
    if not en_hits:
        return f"No significant global social or cashtag chatter recorded for ${ticker}."
    platforms = ", ".join(sorted({h.platform for h in en_hits}))
    mood = "bullish" if score >= 0.20 else ("bearish" if score <= -0.20 else "mixed/neutral")
    return (
        f"Global retail and cashtag sentiment for ${ticker} across {platforms} is {mood} "
        f"(weighted score {score:+.2f} across {len(en_hits)} signal(s))."
    )


def _map_fear_greed_zones(index_0_100: int) -> tuple[FearGreedZone, VietstockPsychologyState]:
    """Map 0..100 composite index to CNN Fear & Greed Zone + Vietstock Market Psychology State."""
    if index_0_100 <= 24:
        return "EXTREME_FEAR", "BI_QUAN_QUA_MUC"
    if index_0_100 <= 44:
        return "FEAR", "THAN_TRONG_SO_HAI"
    if index_0_100 <= 55:
        return "NEUTRAL", "TRUNG_LAP_PHAN_VAN"
    if index_0_100 <= 74:
        return "GREED", "LAC_QUAN_HUNG_PHAN"
    return "EXTREME_GREED", "HUNG_PHAN_QUA_MUC"


def _detect_insider_signal(hits: list[ScoredSentimentHit]) -> InsiderSignal:
    """Finnhub Insider Sentiment (MSPR) / Form 4 & HOSE CBTT insider activity classifier."""
    buy_w = 0.0
    sell_w = 0.0
    for h in hits:
        low = h.text.lower()
        for phrase in _INSIDER_BUY_TERMS:
            if phrase in low and not _is_negated_match(low, phrase):
                buy_w += h.effective_weight
        for phrase in _INSIDER_SELL_TERMS:
            if phrase in low and not _is_negated_match(low, phrase):
                sell_w += h.effective_weight
    if buy_w <= 0.0 and sell_w <= 0.0:
        return "NONE"
    if buy_w >= 1.25 * sell_w and buy_w > 0.0:
        return "NET_BUYING"
    if sell_w >= 1.25 * buy_w and sell_w > 0.0:
        return "NET_SELLING"
    return "MIXED"


def _compute_uncertainty_score(hits: list[ScoredSentimentHit]) -> float:
    """Loughran-McDonald (2011) & Finnhub Filings Sentiment uncertainty/litigation density."""
    if not hits:
        return 0.0
    total_w = sum(h.effective_weight for h in hits)
    if total_w <= 0.0:
        return 0.0
    flagged_w = 0.0
    for h in hits:
        low = h.text.lower()
        if any(
            term in low and not _is_negated_match(low, term)
            for term in _UNCERTAINTY_AND_LITIGATION_TERMS
        ):
            flagged_w += h.effective_weight
    return round(min(1.0, flagged_w / total_w), 4)


def _compute_buzz_velocity(all_hits: list[ScoredSentimentHit]) -> BuzzVelocity:
    """Sentimentick & Finnhub Social Buzz relative mention velocity classifier."""
    if not all_hits:
        return "DORMANT"
    recent = sum(1 for h in all_hits if h.freshness in ("BREAKING_6H", "INTRADAY_24H"))
    older = sum(1 for h in all_hits if h.freshness in ("SHORT_3D", "WEEKLY_7D", "STALE"))
    if recent >= 4 and recent >= 2 * max(1, older):
        return "VIRAL_SURGE"
    if recent >= 2:
        return "ELEVATED"
    if recent >= 1:
        return "NORMAL"
    return "DORMANT"


def _pillar_score_and_status(
    matching_hits: list[ScoredSentimentHit],
) -> tuple[int, PillarStatus, int]:
    """Compute bounded 0..100 Optix-style score and status for one checklist pillar."""
    if not matching_hits:
        return 50, "INSUFFICIENT_DATA", 0
    total_w = sum(h.effective_weight for h in matching_hits)
    if total_w <= 0.0:
        return 50, "NEUTRAL", len(matching_hits)
    avg_pol = sum(h.polarity_score * h.effective_weight for h in matching_hits) / total_w
    score_100 = round(max(0.0, min(100.0, 50.0 + 50.0 * avg_pol)))
    if score_100 >= 58:
        status: PillarStatus = "BULLISH"
    elif score_100 <= 42:
        status = "BEARISH"
    else:
        status = "NEUTRAL"
    return score_100, status, len(matching_hits)


def _blend_quant_pillar(
    base_score: int,
    base_status: PillarStatus,
    base_count: int,
    quant_scores: list[float],
) -> tuple[int, PillarStatus, int]:
    """Blend qualitative text pillar score with real quantitative formula sub-scores."""
    valid = [s for s in quant_scores if s is not None]
    if not valid:
        return base_score, base_status, base_count
    q_mean = sum(valid) / len(valid)
    score = round(0.45 * base_score + 0.55 * q_mean) if base_count > 0 else round(q_mean)
    score = max(0, min(100, score))
    if score >= 58:
        status: PillarStatus = "BULLISH"
    elif score <= 42:
        status = "BEARISH"
    else:
        status = "NEUTRAL"
    return score, status, base_count + len(valid)


def _build_benchmark_standards(
    *,
    factual_hits: list[ScoredSentimentHit],
    official_hits: list[ScoredSentimentHit],
    social_hits: list[ScoredSentimentHit],
    all_hits: list[ScoredSentimentHit],
    official_score: float,
    social_score: float,
    seeding_collapsed_count: int,
    unverified_rumor_count: int,
    ohlcv_metrics: dict[str, Any] | None = None,
    proxy_metrics: dict[str, Any] | None = None,
    fundamentals: dict[str, Any] | None = None,
    nlp_telemetry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Synthesize gold-standard sentiment metrics across the 7 benchmark frameworks."""
    ohlcv = ohlcv_metrics or {}
    proxies = proxy_metrics or {}

    # SentimenTrader Smart Money vs Dumb Money Confidence (0..100)
    # PASS-2 audit P2-5: NO fabricated 50.0 placeholder — when a side has
    # neither hits nor proxy the field stays None (docs/27 zero-fabrication).
    smart_conf: float | None = (
        round(max(0.0, min(100.0, 50.0 + 50.0 * official_score)), 1) if official_hits else None
    )
    smart_proxy = proxies.get("smart_money_proxy_score")
    if smart_proxy is not None:
        smart_conf = (
            round(0.55 * smart_conf + 0.45 * float(smart_proxy), 1)
            if official_hits and smart_conf is not None
            else round(float(smart_proxy), 1)
        )

    dumb_conf: float | None = (
        round(max(0.0, min(100.0, 50.0 + 50.0 * social_score)), 1) if social_hits else None
    )
    crowd_proxy = proxies.get("crowd_positioning_proxy_score")
    if crowd_proxy is not None:
        dumb_conf = (
            round(0.55 * dumb_conf + 0.45 * float(crowd_proxy), 1)
            if social_hits and dumb_conf is not None
            else round(float(crowd_proxy), 1)
        )

    smart_dumb_spread = (
        round(smart_conf - dumb_conf, 1)
        if smart_conf is not None and dumb_conf is not None
        else None
    )

    # PASS-2 P2-5: None-safe contrarian chain — mypy cannot narrow through
    # intermediate booleans, so the None checks stay inline on every branch.
    if (
        dumb_conf is not None
        and smart_conf is not None
        and dumb_conf >= 70.0
        and smart_conf <= 40.0
    ):
        contrarian: ContrarianSignal = "CONTRARIAN_BEARISH_WARNING"
    elif (
        dumb_conf is not None
        and smart_conf is not None
        and dumb_conf <= 30.0
        and smart_conf >= 55.0
    ):
        contrarian = "CONTRARIAN_BULLISH_OPPORTUNITY"
    elif (
        dumb_conf is not None
        and smart_conf is not None
        and (
            (smart_conf >= 58.0 and dumb_conf >= 58.0) or (smart_conf <= 42.0 and dumb_conf <= 42.0)
        )
    ):
        contrarian = "TREND_ALIGNED"
    else:
        contrarian = "NEUTRAL"

    insider_sig = _detect_insider_signal(factual_hits)
    mspr_val = proxies.get("insider_mspr")
    if insider_sig == "NONE" and mspr_val is not None:
        if float(mspr_val) >= 0.25:
            insider_sig = "NET_BUYING"
        elif float(mspr_val) <= -0.25:
            insider_sig = "NET_SELLING"
        else:
            insider_sig = "MIXED"

    uncertainty_score = _compute_uncertainty_score(factual_hits)
    buzz_vel = _compute_buzz_velocity(all_hits)
    s_score = proxies.get("social_s_score")
    if s_score is not None and buzz_vel in ("DORMANT", "NORMAL", "ELEVATED"):
        if float(s_score) >= 2.0:
            buzz_vel = "VIRAL_SURGE"
        elif float(s_score) >= 1.0 and buzz_vel in ("DORMANT", "NORMAL"):
            buzz_vel = "ELEVATED"

    # 7-Pillar Checklist (CNN F&G + Finnhub + Investopedia + Vietstock + SentimenTrader + Quant)
    tier1_hits = [h for h in factual_hits if h.tier == "TIER_1"]
    tier2_hits = [h for h in factual_hits if h.tier == "TIER_2"]
    momentum_hits = [
        h for h in factual_hits if any(t in h.text.lower() for t in _MOMENTUM_MA_TERMS)
    ]
    breadth_hits = [
        h for h in factual_hits if any(t in h.text.lower() for t in _BREADTH_LIQUIDITY_TERMS)
    ]
    vol_hits = [
        h
        for h in factual_hits
        if any(
            t in h.text.lower()
            for t in (*_VOLATILITY_LEVERAGE_TERMS, *_UNCERTAINTY_AND_LITIGATION_TERMS)
        )
    ]

    p1_score, p1_status, p1_cnt = _pillar_score_and_status(tier1_hits or official_hits[:2])
    p1_quant: list[float] = []
    if mspr_val is not None:
        p1_quant.append(max(0.0, min(100.0, (float(mspr_val) + 1.0) * 50.0)))
    p1_score, p1_status, p1_cnt = _blend_quant_pillar(p1_score, p1_status, p1_cnt, p1_quant)
    if fundamentals:
        p1_cnt += 1

    p2_score, p2_status, p2_cnt = _pillar_score_and_status(tier2_hits or official_hits)
    p2_quant = [float(smart_proxy)] if smart_proxy is not None else []
    p2_score, p2_status, p2_cnt = _blend_quant_pillar(p2_score, p2_status, p2_cnt, p2_quant)

    p3_score, p3_status, p3_cnt = _pillar_score_and_status(momentum_hits)
    p3_mom_q = ohlcv.get("momentum_quant_score")
    p3_score, p3_status, p3_cnt = _blend_quant_pillar(
        p3_score, p3_status, p3_cnt, [float(p3_mom_q)] if p3_mom_q is not None else []
    )

    p4_score, p4_status, p4_cnt = _pillar_score_and_status(breadth_hits)
    p4_quant: list[float] = []
    if ohlcv.get("breadth_flow_quant_score") is not None:
        p4_quant.append(float(ohlcv["breadth_flow_quant_score"]))
    if proxies.get("breadth_proxy_score") is not None:
        p4_quant.append(float(proxies["breadth_proxy_score"]))
    p4_score, p4_status, p4_cnt = _blend_quant_pillar(p4_score, p4_status, p4_cnt, p4_quant)

    p5_score, p5_status, p5_cnt = _pillar_score_and_status(vol_hits)
    p5_quant: list[float] = []
    if ohlcv.get("volatility_quant_score") is not None:
        p5_quant.append(float(ohlcv["volatility_quant_score"]))
    if proxies.get("volatility_options_proxy_score") is not None:
        p5_quant.append(float(proxies["volatility_options_proxy_score"]))
    p5_score, p5_status, p5_cnt = _blend_quant_pillar(p5_score, p5_status, p5_cnt, p5_quant)
    if (
        proxies.get("vix_term_regime") == "BACKWARDATION_STRESS"
        or (proxies.get("cboe_skew") is not None and float(proxies["cboe_skew"]) >= 140.0)
    ) and p5_status != "BEARISH":
        p5_status = "WARNING"

    p6_score, p6_status, p6_cnt = _pillar_score_and_status(social_hits)
    p6_quant = [float(crowd_proxy)] if crowd_proxy is not None else []
    p6_score, p6_status, p6_cnt = _blend_quant_pillar(p6_score, p6_status, p6_cnt, p6_quant)

    # Pillar 7: Anti-Seeding & Data Integrity
    penalty = min(50, seeding_collapsed_count * 12 + unverified_rumor_count * 15)
    p7_score = max(0, 100 - penalty) if all_hits else 50
    p7_status: PillarStatus = (
        "WARNING" if (seeding_collapsed_count > 0 or unverified_rumor_count > 0) else "BULLISH"
    )

    pillars: list[dict[str, Any]] = [
        {
            "pillar_id": "filings_earnings_insider",
            "name": "Official Filings, Earnings Call & Insider (MSPR)",
            "standard_ref": "Finnhub Filings/Transcripts/Insider, Form 4 & SEC/HOSE",
            "score_0_100": p1_score,
            "status": p1_status,
            "evidence_count": p1_cnt,
        },
        {
            "pillar_id": "institutional_smart_money",
            "name": "Institutional Research & Smart Money Flow",
            "standard_ref": "SentimenTrader, SqueezeMetrics DIX/GEX & VN Foreign/Prop Flow",
            "score_0_100": p2_score,
            "status": p2_status,
            "evidence_count": p2_cnt,
        },
        {
            "pillar_id": "momentum_and_moving_averages",
            "name": "Price Momentum & Moving Averages (MA50/MA125/MA200/RSI-14)",
            "standard_ref": "Investopedia & CNN Fear & Greed Momentum",
            "score_0_100": p3_score,
            "status": p3_status,
            "evidence_count": p3_cnt,
        },
        {
            "pillar_id": "breadth_strength_and_liquidity",
            "name": "Market Breadth (TRIN/McClellan/52W H-L) & Flow (MFI-14/CMF-21)",
            "standard_ref": "Vietstock/FiinTrade Psychology & CNN Breadth",
            "score_0_100": p4_score,
            "status": p4_status,
            "evidence_count": p4_cnt,
        },
        {
            "pillar_id": "volatility_options_and_leverage",
            "name": "Volatility (VIX/VIX3M/SKEW), Options Put/Call & Margin/Audit Risk",
            "standard_ref": "CBOE Options/Vol, FINRA Margin & Loughran-McDonald",
            "score_0_100": p5_score,
            "status": p5_status,
            "evidence_count": p5_cnt,
        },
        {
            "pillar_id": "retail_social_and_buzz_velocity",
            "name": "Retail Crowd, Surveys (AAII/II/NAAIM) & Buzz S-Score",
            "standard_ref": "AAII/NAAIM, Sentimentick, StockTwits S-Score & Finnhub Social",
            "score_0_100": p6_score,
            "status": p6_status,
            "evidence_count": p6_cnt,
        },
        {
            "pillar_id": "anti_seeding_and_data_integrity",
            "name": "Social Manipulation, Bot/VIP Seeding & Rumor Audit",
            "standard_ref": "ResearchGate 379351322 & MetaSearch Phase 2B",
            "score_0_100": p7_score,
            "status": p7_status,
            "evidence_count": len(all_hits),
        },
    ]

    # Component-bounded Optix / CNN Fear & Greed composite (0..100) across active pillars (1..6)
    active_market_pillars = [p for p in pillars[:6] if p["evidence_count"] > 0]
    if active_market_pillars:
        fg_index = round(
            sum(p["score_0_100"] for p in active_market_pillars) / len(active_market_pillars)
        )
    else:
        # PASS-2 audit P2-5: no active market pillar -> no fabricated 50 —
        # emit None + INSUFFICIENT_DATA per the docs/27 zero-fabrication law.
        fg_index = None
    if fg_index is not None:
        fg_index = max(0, min(100, fg_index))
        fg_zone, vs_state = _map_fear_greed_zones(fg_index)
    else:
        fg_zone, vs_state = "INSUFFICIENT_DATA", "THIEU_DU_LIEU"

    quant_telemetry: dict[str, Any] | None = None
    if ohlcv.get("available") or proxies.get("available") or fundamentals:
        quant_telemetry = {
            "ohlcv": ohlcv if ohlcv.get("available") else None,
            "market_proxies": proxies if proxies.get("available") else None,
            "fundamentals": fundamentals,
        }

    return {
        "fear_greed_index": fg_index,
        "fear_greed_zone": fg_zone,
        "vietstock_psychology_state": vs_state,
        "smart_money_confidence": smart_conf,
        "dumb_money_confidence": dumb_conf,
        "smart_dumb_spread": smart_dumb_spread,
        "contrarian_signal": contrarian,
        "buzz_velocity": buzz_vel,
        "insider_signal": insider_sig,
        "uncertainty_and_litigation_score": uncertainty_score,
        "nlp_telemetry": nlp_telemetry,
        "quantitative_telemetry": quant_telemetry,
        "seven_pillar_checklist": pillars,
    }


def _empty_benchmark_standards() -> dict[str, Any]:
    return {
        # PASS-2 audit P2-5: no fabricated placeholders — None when absent.
        "fear_greed_index": None,
        "fear_greed_zone": "INSUFFICIENT_DATA",
        "vietstock_psychology_state": "THIEU_DU_LIEU",
        "smart_money_confidence": None,
        "dumb_money_confidence": None,
        "smart_dumb_spread": None,
        "contrarian_signal": "NEUTRAL",
        "buzz_velocity": "DORMANT",
        "insider_signal": "NONE",
        "uncertainty_and_litigation_score": 0.0,
        "nlp_telemetry": None,
        "quantitative_telemetry": None,
        "seven_pillar_checklist": [],
    }


def _quant_only_payload(
    entity: EquityEntity,
    *,
    ohlcv_metrics: dict[str, Any],
    proxy_metrics: dict[str, Any],
    fundamentals: dict[str, Any] | None,
    seeding_collapsed_count: int = 0,
    unverified_rumor_count: int = 0,
    all_hits: list[ScoredSentimentHit] | None = None,
) -> dict[str, Any]:
    """Return strictly grounded quantitative sentiment when OHLCV/proxies/FA exist."""
    hits_all = all_hits or []
    vn_social_hits = [s for s in hits_all if s.ecosystem == "vn_social"]
    global_social_hits = [s for s in hits_all if s.ecosystem == "global_social"]
    social_hits = vn_social_hits + global_social_hits
    social_total_w = sum(s.effective_weight for s in social_hits)
    social_score = (
        round(
            max(
                -1.0,
                min(
                    1.0,
                    sum(s.polarity_score * s.effective_weight for s in social_hits)
                    / social_total_w,
                ),
            ),
            4,
        )
        if social_total_w > 0.0
        else 0.0
    )

    bs = _build_benchmark_standards(
        factual_hits=[],
        official_hits=[],
        social_hits=social_hits,
        all_hits=hits_all,
        official_score=0.0,
        social_score=social_score,
        seeding_collapsed_count=seeding_collapsed_count,
        unverified_rumor_count=unverified_rumor_count,
        ohlcv_metrics=ohlcv_metrics,
        proxy_metrics=proxy_metrics,
        fundamentals=fundamentals,
        nlp_telemetry=None,
    )
    fg = bs["fear_greed_index"]
    signed = round((fg - 50) / 50.0, 3) if fg is not None else 0.0
    label = _score_to_label(signed)
    smart_conf = bs["smart_money_confidence"]
    dumb_conf = bs["dumb_money_confidence"]
    inst_vs_retail = (
        round(max(-2.0, min(2.0, (smart_conf - dumb_conf) / 50.0)), 4)
        if smart_conf is not None and dumb_conf is not None
        else 0.0
    )
    breakers: list[str] = []
    if seeding_collapsed_count > 0:
        breakers.append("COORDINATED_SOCIAL_SEEDING")
    if unverified_rumor_count > 0:
        breakers.append("UNVERIFIED_RUMOR_SPIKE")
    if fg is None:
        breakers.append("INSUFFICIENT_DATA")

    has_ohlcv_or_proxy = bool(ohlcv_metrics.get("available") or proxy_metrics.get("available"))
    confidence = 0.35 if has_ohlcv_or_proxy else 0.2
    if fg is None:
        confidence = min(confidence, 0.18)

    vn_platforms = sorted({s.platform for s in vn_social_hits})
    global_platforms = sorted({s.platform for s in global_social_hits})
    audit_note = (
        f"Detected {seeding_collapsed_count} seeding post(s) and "
        f"{unverified_rumor_count} uncorroborated rumor(s) with zero factual text hits; "
        f"score derived strictly from real OHLCV/proxy/fundamental telemetry."
        if (seeding_collapsed_count > 0 or unverified_rumor_count > 0)
        else (
            "Quantitative telemetry only (OHLCV / market proxies / fundamentals); "
            "zero factual social/news hits retrieved or supplied."
        )
    )
    return {
        "module": "omni_channel_equity_sentiment_v2",
        "entity": {
            "ticker": entity.ticker,
            "resolved_name": entity.resolved_name,
            "exchange": entity.exchange,
            "timestamp_utc": entity.timestamp_utc,
        },
        "sentiment_summary": {
            "composite_score": signed,
            "institutional_vs_retail_spread": inst_vs_retail,
            "label": label,
            "confidence": confidence,
            "market_regime": "LOW_SIGNAL_NOISE",
            "momentum_24h": "UNKNOWN",
        },
        "benchmark_standards": bs,
        "network_telemetry": {
            "official_and_media": {
                "weighted_score": 0.0,
                "hit_count": 0,
                "key_catalysts": [],
            },
            "vn_social_ecosystem": {
                "weighted_score": 0.0,
                "platforms_covered": vn_platforms,
                "hit_count": len(vn_social_hits),
                "crowd_narrative_vi": (
                    f"Chưa có bài viết xác thực trên MXH cho {entity.ticker}; "
                    f"điểm tổng hợp lấy từ dữ liệu OHLCV/proxy định lượng thực tế."
                ),
                "trending_slang_vi": [],
            },
            "global_social_ecosystem": {
                "weighted_score": 0.0,
                "platforms_covered": global_platforms,
                "hit_count": len(global_social_hits),
                "crowd_narrative_en": (
                    f"No verified social hits for ${entity.ticker}; "
                    f"scoring reflects real quantitative OHLCV/market proxy formulas."
                ),
                "trending_tags_en": [],
            },
        },
        "social_integrity_audit": {
            "manipulation_or_seeding_detected": seeding_collapsed_count > 0,
            "unverified_rumor_count": unverified_rumor_count,
            "audit_notes": audit_note,
        },
        "top_verified_drivers": [],
        "risk_circuit_breakers": breakers,
    }


def _insufficient_data_payload(entity: EquityEntity) -> dict[str, Any]:
    return {
        "module": "omni_channel_equity_sentiment_v2",
        "entity": {
            "ticker": entity.ticker,
            "resolved_name": entity.resolved_name,
            "exchange": entity.exchange,
            "timestamp_utc": entity.timestamp_utc,
        },
        "sentiment_summary": {
            "composite_score": 0.0,
            "institutional_vs_retail_spread": 0.0,
            "label": "NEUTRAL",
            "confidence": 0.0,
            "market_regime": "LOW_SIGNAL_NOISE",
            "momentum_24h": "UNKNOWN",
        },
        "benchmark_standards": _empty_benchmark_standards(),
        "network_telemetry": {
            "official_and_media": {
                "weighted_score": 0.0,
                "hit_count": 0,
                "key_catalysts": [],
            },
            "vn_social_ecosystem": {
                "weighted_score": 0.0,
                "platforms_covered": [],
                "hit_count": 0,
                "crowd_narrative_vi": (
                    f"Không có dữ liệu xác thực trên mạng xã hội Việt Nam cho {entity.ticker}."
                ),
                "trending_slang_vi": [],
            },
            "global_social_ecosystem": {
                "weighted_score": 0.0,
                "platforms_covered": [],
                "hit_count": 0,
                "crowd_narrative_en": (
                    f"No verified global social data available for ${entity.ticker}."
                ),
                "trending_tags_en": [],
            },
        },
        "social_integrity_audit": {
            "manipulation_or_seeding_detected": False,
            "unverified_rumor_count": 0,
            "audit_notes": (
                "Insufficient verified data across all network layers; zero fabrication enforced."
            ),
        },
        "top_verified_drivers": [],
        "risk_circuit_breakers": ["INSUFFICIENT_DATA"],
    }
