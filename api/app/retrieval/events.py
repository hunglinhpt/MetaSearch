"""Market News Wire & Corporate Events Intelligence (bilingual VI / EN).

Independent financial news-wire aggregation, context-aware entity extraction,
corporate action / ex-dividend calendar parsing, cumulative share-adjustment
verification, and listing/regulatory risk classification.

Design guarantees:
- Zero Playwright / headless-browser runtime: pure async ``httpx`` + stdlib
  ``HTMLParser`` + ``defusedxml.ElementTree`` with 5 MB payload guards.
- Zero fabrication: every field traces to live parsed source records or caller-
  supplied payloads; missing metrics or dates stay ``None``.
- Per-source circuit breakers (``events:{source_id}``) + explicit schema-drift
  warnings whenever an HTML/RSS upstream serves bytes yet yields 0 items.
- Context-aware ticker matching: never blacklists real listed tickers that
  double as macro acronyms (USD, CPI, TPP, CEO); instead suppresses unit /
  FX-pair contexts and requires explicit prefix/prefix-phrase evidence.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import re
import time
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from html import unescape as _html_unescape
from html.parser import HTMLParser
from itertools import combinations
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError as XMLParseError
from defusedxml.ElementTree import fromstring as _xml_fromstring

from app.cache import CacheClient
from app.core.budget import get_current_budget
from app.core.offload import run_cpu
from app.extraction import pinning, ssrf
from app.http import get_http_client
from app.retrieval.adapters.google_news import GoogleNewsAdapter
from app.retrieval.aliases import resolve_alias_tickers
from app.retrieval.fa_sources.base import (
    BROWSER_HEADERS,
    MAX_RESPONSE_BYTES,
    ensure_global_symbol,
    ensure_symbol,
)
from app.retrieval.health import EngineBreakerRegistry, Outcome
from app.retrieval.vn_tickers import (
    _VIET_STOCK_STOP_WORDS,
    get_vn_ticker_symbols,
    is_vn_ticker,
    load_vn_tickers,
)

logger = logging.getLogger(__name__)

# Vietnam trading timezone (UTC+7) used for local calendar ex-date comparisons.
_VN_TZ = timezone(timedelta(hours=7))

_NEWS_WIRE_CACHE_TTL_S = 900  # 15 min
_EVENTS_CACHE_TTL_S = 6 * 3600  # 6 hours

EventScope = Literal["ticker", "market", "policy", "sector", "general"]
NewsCategory = Literal[
    "corporate_action",
    "listing_status",
    "insider_trade",
    "regulatory",
    "macro_policy",
    "market_flow",
    "general_finance",
]
SeverityLevel = Literal["critical", "high", "medium", "info"]

# --------------------------------------------------------------------------------------
# Curated headless news wire & corporate event sources (verified live HTTP 200)
# --------------------------------------------------------------------------------------

RSS_WIRE_SOURCES: tuple[tuple[str, str, NewsCategory, EventScope], ...] = (
    (
        "vietstock_corporate",
        "https://vietstock.vn/733/rss.rss",
        "general_finance",
        "ticker",
    ),
    (
        "vietstock_dividends",
        "https://vietstock.vn/738/rss.rss",
        "corporate_action",
        "ticker",
    ),
    (
        "vietstock_insider",
        "https://vietstock.vn/739/chung-khoan/co-phieu.rss",
        "insider_trade",
        "ticker",
    ),
    (
        "vietstock_listing",
        "https://vietstock.vn/741/chung-khoan/niem-yet.rss",
        "listing_status",
        "ticker",
    ),
    (
        "vietstock_macro",
        "https://vietstock.vn/761/rss.rss",
        "macro_policy",
        "market",
    ),
    (
        "vietstock_policy",
        "https://vietstock.vn/143/rss.rss",
        "macro_policy",
        "policy",
    ),
    (
        "vietstock_stocks",
        "https://vietstock.vn/830/chung-khoan/tin-tuc.rss",
        "general_finance",
        "ticker",
    ),
    (
        "cafef_market",
        "https://cafef.vn/thi-truong-chung-khoan.rss",
        "market_flow",
        "market",
    ),
    (
        "cafef_corporate",
        "https://cafef.vn/doanh-nghiep.rss",
        "general_finance",
        "ticker",
    ),
)

HTML_WIRE_SOURCES: tuple[tuple[str, str, str, NewsCategory, EventScope], ...] = (
    (
        "vneconomy_equities",
        "https://vneconomy.vn/chung-khoan.htm",
        "https://vneconomy.vn",
        "market_flow",
        "market",
    ),
    (
        "stockbiz_news",
        "https://stockbiz.vn/tin-tuc",
        "https://stockbiz.vn",
        "general_finance",
        "ticker",
    ),
)

CAFEF_EVENTS_URL = "https://cafef.vn/du-lieu/Ajax/PageNew/LichSuKien.ashx"
COTUC_CALENDAR_URL = "https://cotuc.vn/su-kien"

# --------------------------------------------------------------------------------------
# Context-aware entity extraction (protects macro acronyms without blacklisting tickers)
# --------------------------------------------------------------------------------------

_PREFIX_TICKER_RE = re.compile(r"^\s*([A-Z0-9]{1,6}(?:[.-][A-Z]{1,3})?)\s*:")
_EXCHANGE_TICKER_RE = re.compile(
    r"\b(?:HOSE|HSX|HNX|UPCOM|NASDAQ|NYSE|AMEX)\s*:\s*([A-Z0-9]{1,6}(?:[.-][A-Z]{1,3})?)\b"
)
_EXPLICIT_PHRASE_RE = re.compile(
    r"(?i:cổ\s+phiếu|mã(?:\s+chứng\s+khoán|\s+ck)?)\s+([A-Z0-9]{1,6}(?:[.-][A-Z]{1,3})?)\b"
)
_CASHTAG_RE = re.compile(r"\$([A-Z]{1,6}(?:[.-][A-Z]{1,3})?)\b")
_BARE_TICKER_RE = re.compile(r"\b([A-Z]{3}|[A-Z]{2}\d|[A-Z]\d[A-Z]|[A-Z]\d{2})\b")
_ALPHA_WORD_RE = re.compile(r"[A-Za-zÀ-ỹ]+")


@lru_cache(maxsize=256)
def _symbol_boundary_regex(sym: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(sym)}(?![A-Za-z0-9])")


# Bounded lookbehind ReDoS-safe unit matcher (applied to `.rstrip()` of <=48 chars before match).
_UNIT_BEFORE_RE = re.compile(
    r"(?:\d[\d.,]*(?:\s*%)?|\btỷ|\btriệu|\bnghìn|\bngàn|\btrăm)\Z",
    re.IGNORECASE,
)
_SLASH_BEFORE_RE = re.compile(r"/\s*$")
_SLASH_AFTER_RE = re.compile(r"^\s*/")

_PREFIX_CORP_CONTEXT: tuple[str, ...] = (
    "ctcp",
    "công ty",
    "tổng công ty",
    "tập đoàn",
    "ngân hàng",
    "hđqt",
    "cổ phiếu",
    "cổ tức",
    "chứng khoán",
    "hose",
    "hsx",
    "hnx",
    "upcom",
    "nghị quyết",
    "bctc",
    "kqkd",
    "lợi nhuận",
    "doanh thu",
    "kết quả",
    "chi trả",
    "tạm ứng",
    "mua ròng",
    "bán ròng",
    "tăng vốn",
    "đại hội",
    "thâu tóm",
    "chia thưởng",
    "giải trình",
    "cảnh báo",
    "kiểm soát",
    "đình chỉ",
    "phát hành",
    "niêm yết",
    "chốt quyền",
    "gdkhq",
    "inc",
    "corp",
    "ltd",
    "plc",
    "earnings",
    "revenue",
    "profit",
    "guidance",
    "dividend",
    "buyback",
    "split",
    "stock",
)

# Real listed symbols or common macro/English/VN acronyms requiring strong context
# ("TICKER: <corporate>", "co phieu TICKER", "$TICKER", "HOSE: TICKER") before binding.
# High-cap VN tickers BID/GAS/POW/TRA/NHA are intentionally kept out of this unconditional
# blocklist; GAS/POW use targeted commodity/crypto disambiguation instead.
_AMBIGUOUS_ACRONYMS: frozenset[str] = frozenset(
    (
        _VIET_STOCK_STOP_WORDS
        | {
            "AGM",
            "API",
            "APP",
            "AVG",
            "BIG",
            "BOD",
            "BOT",
            "CAD",
            "CAP",
            "CAR",
            "CEO",
            "CFO",
            "CLI",
            "CNY",
            "COM",
            "CPI",
            "CSV",
            "DAT",
            "DID",
            "DOC",
            "ECO",
            "EPS",
            "ESG",
            "ETF",
            "EUR",
            "FDI",
            "FED",
            "FIT",
            "FOX",
            "G20",
            "GDP",
            "GTC",
            "HAD",
            "HAN",
            "HAS",
            "HOM",
            "HOT",
            "IMF",
            "IPO",
            "ITS",
            "JPY",
            "LAI",
            "LAW",
            "LLM",
            "MAC",
            "MCP",
            "NAV",
            "NET",
            "NEW",
            "NIM",
            "ODA",
            "OIL",
            "OMO",
            "ONE",
            "PEG",
            "PHP",
            "PMI",
            "PPI",
            "PRO",
            "ROA",
            "ROE",
            "SAC",
            "SAM",
            "SBV",
            "SDK",
            "SEA",
            "SEC",
            "SEP",
            "SGD",
            "SHE",
            "SSC",
            "SSH",
            "TAB",
            "TET",
            "THB",
            "THU",
            "TIN",
            "TOP",
            "TPP",
            "USA",
            "USD",
            "VAT",
            "VCP",
            "VET",
            "VIE",
            "VIP",
            "VND",
            "VUA",
            "WTO",
        }
    )
    - {"BID", "GAS", "NHA", "POW", "TRA"}
)

_MARKET_MACRO_KEYWORDS: tuple[str, ...] = (
    "vn-index",
    "vnindex",
    "hnx-index",
    "thị trường",
    "dòng tiền",
    "khối ngoại",
    "lãi suất",
    "tỷ giá",
    "vĩ mô",
    "chính sách",
    "ngân hàng nhà nước",
    "nhnn",
    "fed",
    "fomc",
    "cpi",
    "gdp",
    "lạm phát",
    "inflation",
    "central bank",
    "rate cut",
    "rate hike",
    "etf",
)

# --------------------------------------------------------------------------------------
# Event classification tables & regexes
# --------------------------------------------------------------------------------------

_WITHOUT_PER_SHARE_UNIT_RE = re.compile(r"/\s*cổ\s*phiếu\b", re.IGNORECASE)
_SHARE_CHANGING_RE = re.compile(
    r"cổ\s*tức(?:\s+\S+){0,4}\s*(?:bằng\s+)?cổ\s*phiếu|cổ\s*phiếu\s*thưởng|thưởng.*cổ\s*phiếu|"
    r"cổ\s*tức[^.;\n]{0,80}?\b\d+(?:[.,]\d+)?\s*%\s*(?:bằng\s+)?cổ\s*phiếu|"
    r"thưởng\s*bằng\s*cổ\s*tức|quyền\s*mua|"
    r"(?:tách|chia\s*tách)(?:\s+\S+){0,3}\s*(?:cổ\s*phiếu|cp|1\s*[:\-]|1\s+thành)|"
    r"\bgộp\s+(?:\S+\s+){0,4}(?:cổ\s*phiếu|cổ\s*phần|cp)\b|chia\s*tách|phát\s*hành|bán\s*ưu\s*đãi|"
    r"stock\s+dividend|bonus\s+(?:shares|issue)|rights\s+(?:issue|offering)|"
    r"(?:forward|reverse)\s+(?:stock\s+)?split|stock\s+split|\d+-for-\d+",
    re.IGNORECASE,
)
_REVERSE_SPLIT_RE = re.compile(
    r"\bgộp\s+(?:[\w.,:-]+\s+){0,4}(?:cổ\s*phiếu|cổ\s*phần|cp)\b|"
    r"\breverse\s+(?:stock\s+)?split\b",
    re.IGNORECASE,
)
_FORWARD_SPLIT_RE = re.compile(
    r"(?:chia\s*tách|tách\s+(?:[\d.,]+\s+)?(?:cổ\s*phiếu|cổ\s*phần|cp)|"
    r"tách\s+(?:tỷ\s+lệ\s+)?1\s*[:\-]\s*\d)|"
    r"forward\s+(?:stock\s+)?split|stock\s+split|\d+-for-\d+|"
    r"\b1\s+(?:(?:cổ\s*phiếu|cổ\s*phần|cp|shares?)\s+)?thành\s+\d+(?:[.,]\d+)?",
    re.IGNORECASE,
)
_STOCK_PCT_RE = re.compile(
    r"(?:cổ\s*tức(?:\s+năm\s+\d{4}|\s+đợt\s+\d+|\s+bằng\s+tiền\s+và)?\s*(?:bằng\s+)?cổ\s*phiếu|"
    r"cổ\s*phiếu\s*thưởng|cổ\s*phiếu\s*:|thưởng\s*(?:bằng\s+)?cổ\s*phiếu|"
    r"bonus\s+shares|stock\s+dividend)"
    r"(?:(?![;\n,/]|(?:\.(?=\s|$)))[^;\n,/]){0,45}?\b(\d+(?:[.,]\d+)?)\s*%"
    r"(?!\s*(?:tiền\s*mặt|bằng\s+tiền|cash))",
    re.IGNORECASE,
)
_STOCK_PCT_PRE_RE = re.compile(
    r"\b(\d+(?:[.,]\d+)?)\s*%\s*(?:(?:là\s+|bằng\s+)?cổ\s*tức(?:\s+năm\s+\d{4})?\s*"
    r"(?:bằng\s+)?cổ\s*phiếu|cổ\s*phiếu(?:\s+thưởng)?|bằng\s+cổ\s*phiếu|"
    r"stock\s+dividend|bonus\s+shares)",
    re.IGNORECASE,
)
_FOR_SPLIT_RE = re.compile(
    r"\b(\d+(?:[.,]\d+)?)\s*(?:(?:cổ\s*phiếu|cổ\s*phần|cp|shares?)\s*)?"
    r"(-for-|thành)\s*(\d+(?:[.,]\d+)?)\b"
    r"(?!\s*(?:%|triệu|tỷ|nghìn|đồng|vnd))",
    re.IGNORECASE,
)
_CLOCK_STRIP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:lúc|vào\s+lúc|vào|tại|thời\s+gian|cập\s+nhật|update[d]?|\bat\b)\s*(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:từ|đến)\s*(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?\s*(?=(?:đến|tới|giờ|am|pm)\b)",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?=\s|$|[|),.-])"),
    re.compile(r"\(\s*(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?\s*\)"),
    re.compile(
        r"\b(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?\s*(?:ngày|am|pm|giờ|utc|gmt)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b0\d:[0-5]\d(?::[0-5]\d)?\b"),
    re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d:[0-5]\d\b"),
)
_DATE_TIME_COLON_RE = re.compile(
    r"(\b(?:\d{1,2}/\d{1,2}/20\d{2}|20\d{2}-\d{2}-\d{2})[,;\s]+)"
    r"(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?\b"
)
_RATIO_COLON_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*:\s*(\d+(?:[.,]\d+)?)")
_PROSE_COLON_BEFORE_RE = re.compile(
    r"\b(?:năm|đợt|lần|lô|kỳ|quý|giai\s+đoạn|tranche|phase|stage|round|year)\s*\Z",
    re.IGNORECASE,
)
_PROSE_COLON_AFTER_RE = re.compile(
    r"^\s*(?:%|cp\b|cổ\s+phiếu\b|shares?\b|tỷ\b|triệu\b|nghìn\b|đồng\b|đ\b|vnd\b)",
    re.IGNORECASE,
)
_SHARES_COUNT_RE = re.compile(
    r"([\d.,]+)\s*(?:cp|cổ\s+phiếu|shares?)\b",
    re.IGNORECASE,
)
_CASH_PCT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
_CLAUSE_SPLIT_RE = re.compile(
    r"[;\n]+|\.(?=\s|$)|\s*/\s*(?=(?:\d|\s*(?:cổ\s*phiếu|cổ\s*tức|tiền|thưởng|cash|bonus|stock)))|"
    r",\s*(?=(?:\d+(?:[.,]\d+)?\s*%|cổ\s*tức|cổ\s*phiếu|thưởng|phát\s*hành|chi\s*trả|tạm\s*ứng|bằng\s+|đồng\s+thời|kèm\b))|"
    r"\bvà\b|\bkèm\b",
    re.IGNORECASE,
)
_TAX_PCT_PHRASE_RE = re.compile(
    r"(?:(?:đã\s+)?(?:khấu\s*trừ|trừ|chịu)\s+)?thuế(?:\s+tncn)?[^,;)\n]{0,30}?\d+(?:[.,]\d+)?\s*%|\btax[^,;)\n]{0,20}?\d+(?:[.,]\d+)?\s*%",
    re.IGNORECASE,
)
_CASH_VND_RE = re.compile(
    r"\+?([\d.,]+)\s*(?:đồng|đ|vnd)(?:\s*/\s*(?:cp|cổ\s*phiếu))?(?=\s|$|[),;.])",
    re.IGNORECASE,
)
_NON_PAYOUT_VND_CONTEXT_RE = re.compile(
    r"(?:mệnh\s+giá|giá\s+(?:phát\s+hành|chào\s+bán|bán|mua|ưu\s+đãi|thực\s+hiện|"
    r"tham\s+chiếu|trúng\s+thầu)|vốn\s+điều\s+lệ|tổng\s+giá\s+trị|quy\s+mô|"
    r"\bpar\s+value\b|\bface\s+value\b|\b(?:issue|offering|offer|exercise|subscription)\s+price\b)",
    re.IGNORECASE,
)

_DMY_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b")
_ISO_DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_MS_DATE_RE = re.compile(r"/Date\((-?\d{10,})(?:[+-]\d{4})?\)/")
_DDOC_ID_RE = re.compile(r"dDocName=([A-Za-z0-9_-]+)", re.IGNORECASE)

_VOLATILE_TS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b"),
    re.compile(r"\((?:cập\s+nhật|updated)[^)]*\)", re.IGNORECASE),
)

_LISTING_REINSTATEMENT_RE = (
    "ra khỏi diện",
    "giao dịch trở lại",
    "khắc phục xong",
    "reinstated",
    "removed from warning",
)
_LISTING_NEGATED_REINSTATEMENT = (
    "chưa khắc phục",
    "không khắc phục",
    "nếu chưa",
    "chưa được đưa ra khỏi diện",
    "chưa ra khỏi diện",
    "không đưa",
    "từ chối đưa",
    "không chấp thuận đưa",
    "chưa được giao dịch trở lại",
    "tiếp tục bị đình chỉ",
    "tiếp tục đình chỉ",
    "từng được đưa ra khỏi",
    "not reinstated",
    "chưa đủ điều kiện",
)
_LISTING_CONTINUING_RISK_RE = (
    "nhưng vẫn",
    "vẫn giữ diện",
    "giữ nguyên diện",
    "tiếp tục diện",
    "vẫn thuộc diện",
    "chuyển sang diện",
    "tiếp tục bị cảnh báo",
    "tiếp tục kiểm soát",
)
_LISTING_RULE_ORDER: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "delisting",
        (
            "hủy niêm yết",
            "huỷ niêm yết",
            "hủy bỏ niêm yết",
            "huỷ bỏ niêm yết",
            "chấm dứt niêm yết",
            "delist",
            "delisting",
        ),
    ),
    (
        "warning_control",
        (
            "cảnh báo",
            "kiểm soát",
            "đình chỉ",
            "hạn chế giao dịch",
            "tạm ngừng giao dịch",
            "tạm ngưng giao dịch",
            "ngừng giao dịch",
            "ngưng giao dịch",
            "trading halt",
            "suspension",
            "warning status",
        ),
    ),
    (
        "additional_listing",
        (
            "niêm yết bổ sung",
            "niêm yết thêm",
            "đăng ký niêm yết bổ sung",
            "thay đổi đăng ký niêm yết",
            "additional listing",
            "additional shares",
        ),
    ),
    (
        "initial_listing",
        (
            "lần đầu",
            "chào sàn",
            "lễ niêm yết",
            "chấp thuận niêm yết",
            "initial listing",
            "debut",
        ),
    ),
)

_REGULATORY_RULE_ORDER: tuple[tuple[str, int, SeverityLevel, tuple[str, ...]], ...] = (
    (
        "sanction",
        3,
        "critical",
        (
            "xử phạt",
            "thu hồi",
            "đình chỉ",
            "hủy bỏ niêm yết",
            "huỷ bỏ niêm yết",
            "hủy bỏ tư cách",
            "huỷ bỏ tư cách",
            "tước quyền",
            "vi phạm",
            "phạt tiền",
            "enforcement",
            "revocation",
            "sanction",
            "fined",
        ),
    ),
    (
        "warning",
        2,
        "high",
        ("cảnh báo", "nhắc nhở", "khắc phục", "chấn chỉnh", "deficiency notice", "censure"),
    ),
    # Statistics MUST precede listing_notice so aggregate exchange volume/listing stats
    # are not misclassified as an issuer's IPO/new-listing event.
    (
        "statistics",
        0,
        "info",
        (
            "khối lượng chứng khoán",
            "số lượng doanh nghiệp",
            "thống kê",
            "hợp đồng tương lai",
            "đến cuối tháng",
            "chứng khoán lưu ký",
            "market statistics",
        ),
    ),
    (
        "listing_notice",
        1,
        "medium",
        (
            "lễ niêm yết",
            "niêm yết cổ phiếu",
            "chấp thuận niêm yết",
            "chấp thuận đăng ký giao dịch",
            "listing approval",
        ),
    ),
    (
        "licensing",
        0,
        "info",
        (
            "cấp giấy phép",
            "điều chỉnh giấy phép",
            "chấp thuận thành lập",
            "cấp chứng chỉ",
            "gia hạn",
            "license granted",
        ),
    ),
)


# --------------------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ListingClassification:
    subtype: Literal[
        "delisting",
        "warning_control",
        "initial_listing",
        "additional_listing",
        "reinstated",
        "other",
    ]
    severity: SeverityLevel
    is_dilution: bool
    is_risk_alert: bool
    shares_delta: float | None = None


@dataclass(frozen=True, slots=True)
class RegulatoryClassification:
    subtype: Literal[
        "sanction",
        "warning",
        "statistics",
        "listing_notice",
        "licensing",
        "other",
    ]
    risk_level: int
    severity: SeverityLevel


@dataclass(frozen=True, slots=True)
class InsiderClassification:
    direction: Literal["buy", "sell", "transfer", "unknown"]
    execution_status: Literal["executed", "planned", "cancelled", "unknown"]
    shares_count: float | None = None


@dataclass(slots=True)
class ParsedNewsItem:
    title: str
    url: str | None
    source_id: str
    snippet: str = ""
    published_at: datetime | None = None
    event_date: str | None = None
    symbols: list[str] = field(default_factory=list)
    scope: EventScope = "general"
    category: NewsCategory = "general_finance"
    severity: SeverityLevel = "info"
    stable_id: str = ""
    fingerprint: str = ""
    date_status: Literal["event_date", "published_at", "undated"] = "undated"
    duplicate_count: int = 1
    also_reported_by: list[str] = field(default_factory=list)
    is_new: bool = True


@dataclass(slots=True)
class CorporateEventRecord:
    event_id: str
    symbol: str | None
    exchange: str | None
    event_type: Literal[
        "corporate_action",
        "dividend_calendar",
        "listing",
        "insider",
        "regulatory",
    ]
    event_subtype: str
    severity: SeverityLevel
    risk_level: int
    title: str
    detail: str = ""
    event_date: str | None = None
    ex_date: str | None = None
    record_date: str | None = None
    payment_date: str | None = None
    days_until: int | None = None
    source_id: str = ""
    source_url: str | None = None
    is_share_changing: bool = False
    share_factor: float | None = None
    shares_delta: float | None = None
    is_dilution: bool = False
    cash_dividend_pct: float | None = None
    cash_per_share_vnd: float | None = None
    stock_ratio: str | None = None
    date_mismatch_warning: str | None = None
    is_new: bool = True


@dataclass(slots=True)
class ShareRatioExplanation:
    explained: bool
    observed_ratio: float
    matched_factor: float | None = None
    residual_error_pct: float | None = None
    matched_events: list[dict[str, Any]] = field(default_factory=list)
    explanation: str = ""


# --------------------------------------------------------------------------------------
# Pure text, URL, fingerprinting & ticker extraction helpers
# --------------------------------------------------------------------------------------


def _safe_http_url(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned or len(cleaned) > 2048:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        return None
    try:
        parsed = urlsplit(cleaned)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https"):
        return None
    if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
        return None
    if not parsed.hostname:
        return None
    if port is not None and (port < 1 or port > 65535):
        return None
    return cleaned


def _safe_date_ordinal(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        return date.fromisoformat(raw).toordinal()
    except ValueError:
        return 0


def canonical_url(url: str | None) -> str:
    """Normalize a URL for deduplication (strip query tracking + trailing slash)."""
    safe = _safe_http_url(url)
    if not safe:
        return ""
    parsed = urlsplit(safe)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or "/"
    query = "&".join(
        p
        for p in parsed.query.split("&")
        if p and not p.lower().startswith(("utm_", "fbclid=", "gclid=", "ref="))
    )
    return urlunsplit(("https", host, path, query, ""))


def content_fingerprint(text: str) -> str:
    """SHA-256 prefix over text stripped of volatile timestamps and punctuation."""
    normalized = text or ""
    for pattern in _VOLATILE_TS_PATTERNS:
        normalized = pattern.sub(" ", normalized)
    normalized = re.sub(r"[^\w\s]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def stable_document_id(
    url: str | None,
    *,
    source_id: str,
    symbol: str | None = None,
    fallback_text: str = "",
) -> str:
    """Derive a stable <=64-char document ID from URL ``dDocName``, path, or content hash."""
    safe = _safe_http_url(url)
    prefix = (source_id or "src")[:20]
    if safe:
        m_doc = _DDOC_ID_RE.search(safe)
        if m_doc:
            doc_id = m_doc.group(1)
            candidate = f"{prefix}:{doc_id}"
            if len(candidate) <= 64:
                return candidate
            doc_hash = hashlib.sha256(doc_id.encode("utf-8")).hexdigest()[:16]
            return f"{prefix}:{doc_id[:20]}:{doc_hash}"[:64]
        return f"{prefix}:{hashlib.sha256(canonical_url(safe).encode('utf-8')).hexdigest()[:16]}"
    fp = content_fingerprint(fallback_text)
    sym_part = (symbol or "").upper()[:12]
    return f"{prefix}:{sym_part}:{fp[:12]}"[:64] if sym_part else f"{prefix}:{fp[:16]}"[:64]


def _parse_text_date(raw: Any) -> str | None:
    """Parse unambiguous ``YYYY-MM-DD`` or ``DD/MM/YYYY`` into ISO date string."""
    text = str(raw or "").strip()
    if not text:
        return None
    m_iso = _ISO_DATE_RE.search(text)
    if m_iso:
        y, mo, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            pass
    m_dmy = _DMY_DATE_RE.search(text)
    if m_dmy:
        d, mo, y = int(m_dmy.group(1)), int(m_dmy.group(2)), int(m_dmy.group(3))
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            pass
    return None


def _parse_ms_json_date(raw: Any) -> str | None:
    """Convert ``/Date(1782406800000)/`` millisecond timestamps into ISO YYYY-MM-DD."""
    m = _MS_DATE_RE.search(str(raw or ""))
    if not m:
        return None
    try:
        ms = int(m.group(1))
        return datetime.fromtimestamp(ms / 1000.0, tz=_VN_TZ).date().isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def _parse_locale_number(raw: str) -> float | None:
    """Parse VN/EN share or VND strings such as ``104,042,344``, ``1.700.000``, or ``1.500,5``."""
    s = (raw or "").strip()
    if not s:
        return None
    dots = s.count(".")
    commas = s.count(",")
    if dots > 1 and commas == 0:
        cleaned = s.replace(".", "")
    elif commas > 1 and dots == 0:
        cleaned = s.replace(",", "")
    elif dots >= 1 and commas == 1:
        cleaned = (
            s.replace(".", "").replace(",", ".")
            if s.rfind(",") > s.rfind(".")
            else s.replace(",", "")
        )
    elif commas >= 1 and dots == 1:
        cleaned = (
            s.replace(",", "")
            if s.rfind(".") > s.rfind(",")
            else s.replace(".", "").replace(",", ".")
        )
    elif dots == 1 and commas == 0:
        left, right = s.split(".", 1)
        left_digits = left.lstrip("+-")
        cleaned = (
            (left + right)
            if len(right) == 3
            and 1 <= len(left_digits) <= 3
            and left_digits.isdigit()
            and left_digits != "0"
            and not left_digits.startswith("0")
            else s
        )
    elif commas == 1 and dots == 0:
        left, right = s.split(",", 1)
        left_digits = left.lstrip("+-")
        cleaned = (
            (left + right)
            if len(right) == 3
            and 1 <= len(left_digits) <= 3
            and left_digits.isdigit()
            and left_digits != "0"
            and not left_digits.startswith("0")
            else f"{left}.{right}"
        )
    else:
        cleaned = s.replace(",", "")
    try:
        val = float(cleaned)
        return val if math.isfinite(val) else None
    except ValueError:
        return None


def _is_all_caps_headline(text: str) -> bool:
    alpha_words = [w for w in _ALPHA_WORD_RE.findall(text) if len(w) >= 2]
    if len(alpha_words) < 4:
        return False
    upper_words = sum(1 for w in alpha_words if w == w.upper() and any(c.isalpha() for c in w))
    return (upper_words / len(alpha_words)) >= 0.7


def extract_tickers_from_text(
    text: str,
    *,
    allowed_symbols: set[str] | frozenset[str] | None = None,
    extra_known: set[str] | frozenset[str] | None = None,
) -> list[str]:
    """Extract equity tickers from bilingual text with unit/acronym disambiguation."""
    src = text or ""
    if not src.strip():
        return []
    vn_catalog = get_vn_ticker_symbols()
    known = vn_catalog | {s.upper() for s in extra_known} if extra_known else vn_catalog
    upper_allowed = {s.upper() for s in allowed_symbols} if allowed_symbols else set()
    all_caps = _is_all_caps_headline(src)
    low_src = src.lower()

    def _is_known(sym: str) -> bool:
        if upper_allowed and sym in upper_allowed:
            return True
        if sym in _AMBIGUOUS_ACRONYMS:
            return sym in vn_catalog or sym in upper_allowed
        return sym in known

    def _has_unit_or_slash(start_idx: int, end_idx: int) -> bool:
        win_start = max(0, start_idx - 48)
        before = src[win_start:start_idx]
        after = src[end_idx : min(len(src), end_idx + 16)]
        if _UNIT_BEFORE_RE.search(before.rstrip()):
            return True
        return bool(_SLASH_BEFORE_RE.search(before) or _SLASH_AFTER_RE.match(after))

    def _is_commodity_or_crypto_collision(sym: str, start_idx: int = 0, end_idx: int = 0) -> bool:
        if sym == "BID":
            before = src[max(0, start_idx - 24) : start_idx].lower()
            after = src[end_idx : min(len(src), end_idx + 24)].lower()
            if re.match(r"^\s*[-/]\s*ask\b", after) or re.search(r"\bask\s*[-/]\s*\Z", before):
                return True
            if all_caps and not any(ord(c) > 127 for c in src):
                has_ma_bid_ctx = any(
                    k in low_src
                    for k in (
                        "acquisition bid",
                        "takeover bid",
                        "buyout bid",
                        "winning bid",
                        "counter bid",
                        "counter-bid",
                        "higher bid",
                        "lower bid",
                        "bid for ",
                        "bid price",
                    )
                )
                has_vn_bidv_ctx = any(
                    k in low_src
                    for k in (
                        "bidv",
                        "hose",
                        "hsx",
                        "vn30",
                        "vn-index",
                        "ngan hang",
                        "vcb",
                        "ctg",
                    )
                )
                if has_ma_bid_ctx and not has_vn_bidv_ctx:
                    return True
            return False
        if sym == "GAS":
            has_energy_ctx = any(
                k in low_src
                for k in (
                    "natural gas",
                    "henry hub",
                    "oil & gas",
                    "oil and gas",
                    "lng",
                    "khí đốt",
                    "giá gas",
                    "gasoline",
                    "gas price",
                )
            )
            has_equity_ctx = any(
                k in low_src
                for k in (
                    "pv gas",
                    "tổng công ty khí",
                    "petrovietnam",
                    "pvn",
                    "hose: gas",
                    "hsx: gas",
                    "cổ phiếu gas",
                    "mã gas",
                    "cổ tức",
                    "vn30",
                    "vn-index",
                    "khối ngoại",
                )
            )
            return has_energy_ctx and not has_equity_ctx
        if sym == "POW":
            has_crypto_ctx = any(
                k in low_src
                for k in (
                    "proof of work",
                    "proof-of-work",
                    "bitcoin",
                    "ethereum",
                    "blockchain",
                    "hashrate",
                    "mining",
                )
            )
            has_equity_ctx = any(
                k in low_src
                for k in (
                    "pv power",
                    "điện lực dầu khí",
                    "nhơn trạch",
                    "hose: pow",
                    "hsx: pow",
                    "cổ phiếu pow",
                    "mã pow",
                    "cổ tức",
                    "vn30",
                    "vn-index",
                )
            )
            return has_crypto_ctx and not has_equity_ctx
        return False

    found: list[str] = []

    def _add(sym: str) -> None:
        if sym and sym not in found:
            found.append(sym)

    # 1. Explicit headline tag prefix: "TMS: ..."
    m_pref = _PREFIX_TICKER_RE.match(src)
    if m_pref:
        cand = m_pref.group(1).upper()
        rest_low = src[m_pref.end() :].lower()
        ambiguous_ok = (cand not in _AMBIGUOUS_ACRONYMS) or any(
            ctx in rest_low for ctx in _PREFIX_CORP_CONTEXT
        )
        if ambiguous_ok and (is_vn_ticker(cand) or cand in vn_catalog or cand in upper_allowed):
            _add(cand)

    # 1b. Exchange-qualified tickers: "HOSE: BID", "(HSX: GAS)", "NASDAQ: NVDA"
    for m_exch in _EXCHANGE_TICKER_RE.finditer(src):
        full_ex = m_exch.group(0).split(":", 1)[0].strip().upper()
        cand = m_exch.group(1).upper()
        if cand.isdigit() or cand in _AMBIGUOUS_ACRONYMS:
            continue
        if full_ex in ("HOSE", "HSX", "HNX", "UPCOM"):
            if is_vn_ticker(cand) or cand in vn_catalog or cand in upper_allowed:
                _add(cand)
        elif not upper_allowed or cand in upper_allowed:
            _add(cand)

    # 2. Explicit cashtags ($NVDA, $BRK.B, $F)
    for m_cash in _CASHTAG_RE.finditer(src):
        cand = m_cash.group(1).upper()
        if not upper_allowed or cand in upper_allowed or cand in vn_catalog:
            _add(cand)

    # 3. Explicit "co phieu XYZ" / "ma XYZ" phrases (ticker token MUST be uppercase)
    for m_exp in _EXPLICIT_PHRASE_RE.finditer(src):
        cand = m_exp.group(1).upper()
        if cand == "IPO":
            continue
        if len(cand) < 3 and cand not in upper_allowed:
            continue
        if _is_known(cand):
            _add(cand)

    # 4. Bare listed tokens (guarded against units, FX slashes, and acronyms)
    has_corp_ctx = any(ctx in low_src for ctx in _PREFIX_CORP_CONTEXT)
    for m_bare in _BARE_TICKER_RE.finditer(src):
        cand = m_bare.group(1).upper()
        if not _is_known(cand):
            continue
        if _has_unit_or_slash(m_bare.start(), m_bare.end()):
            continue
        if cand in _AMBIGUOUS_ACRONYMS:
            continue
        if _is_commodity_or_crypto_collision(cand, m_bare.start(), m_bare.end()):
            continue
        if all_caps and not has_corp_ctx and len(cand) <= 3 and not any(c.isdigit() for c in cand):
            continue
        _add(cand)

    # 5. Non-3-char global symbols requested explicitly in allowed_symbols (NVDA, TSLA, BRK.B, F)
    gl_hits: list[tuple[int, str]] = []
    for sym in sorted(upper_allowed):
        if sym in found or _BARE_TICKER_RE.fullmatch(sym) or sym in _AMBIGUOUS_ACRONYMS:
            continue
        pat = _symbol_boundary_regex(sym)
        for m_gl in pat.finditer(src):
            if not _has_unit_or_slash(m_gl.start(), m_gl.end()):
                gl_hits.append((m_gl.start(), sym))
                break
    for _pos, sym in sorted(gl_hits):
        _add(sym)

    # Stage B (docs/30): canonical bilingual company/brand alias resolver —
    # "Vinamilk"→VNM, "Địa ốc Hoàng Quân"→HQC, "Thép Hòa Phát"→HPG, … High-
    # precision by construction (unambiguous registry remainders + curated
    # brands); merged after the structural passes, filtered by the same
    # allowed-symbols contract.
    alias_hits = resolve_alias_tickers(src)
    for alias_sym in alias_hits:
        if upper_allowed and alias_sym not in upper_allowed:
            continue
        _add(alias_sym)

    if upper_allowed:
        return [s for s in found if s in upper_allowed]
    return found


def is_market_macro_text(text: str) -> bool:
    """True when text discusses broad market, flows, rates, FX, or macro policy."""
    low = (text or "").lower()
    return any(kw in low for kw in _MARKET_MACRO_KEYWORDS)


# --------------------------------------------------------------------------------------
# Event & risk classifiers
# --------------------------------------------------------------------------------------


def _has_positive_reinstatement(low: str) -> bool:
    return (
        any(k in low for k in _LISTING_REINSTATEMENT_RE)
        and not any(k in low for k in _LISTING_CONTINUING_RISK_RE)
        and not any(k in low for k in _LISTING_NEGATED_REINSTATEMENT)
    )


def classify_listing_event(title: str, detail: str = "") -> ListingClassification:
    """Classify exchange listing events while distinguishing positive reinstatements."""
    text = f"{title} {detail}".strip()
    low = text.lower()
    has_delisting = any(k in low for k in _LISTING_RULE_ORDER[0][1])
    if not has_delisting and _has_positive_reinstatement(low):
        return ListingClassification(
            subtype="reinstated",
            severity="info",
            is_dilution=False,
            is_risk_alert=False,
        )
    shares_delta: float | None = None
    m_sh = _SHARES_COUNT_RE.search(text)
    if m_sh:
        shares_delta = _parse_locale_number(m_sh.group(1))

    for subtype, keywords in _LISTING_RULE_ORDER:
        if any(k in low for k in keywords):
            if subtype == "delisting":
                return ListingClassification(
                    subtype="delisting",
                    severity="critical",
                    is_dilution=False,
                    is_risk_alert=True,
                    shares_delta=shares_delta,
                )
            if subtype == "warning_control":
                return ListingClassification(
                    subtype="warning_control",
                    severity="high",
                    is_dilution=False,
                    is_risk_alert=True,
                    shares_delta=shares_delta,
                )
            if subtype == "initial_listing":
                return ListingClassification(
                    subtype="initial_listing",
                    severity="medium",
                    is_dilution=False,
                    is_risk_alert=False,
                    shares_delta=shares_delta,
                )
            sev: SeverityLevel = (
                "high" if (shares_delta is not None and shares_delta >= 5_000_000) else "medium"
            )
            return ListingClassification(
                subtype="additional_listing",
                severity=sev,
                is_dilution=True,
                is_risk_alert=False,
                shares_delta=shares_delta,
            )

    return ListingClassification(
        subtype="other",
        severity="info",
        is_dilution=False,
        is_risk_alert=False,
        shares_delta=shares_delta,
    )


def classify_regulatory_action(title: str, summary: str = "") -> RegulatoryClassification:
    """Severity-first classification for regulator & exchange disclosures."""
    low = f"{title} {summary}".lower()
    has_reinstatement = _has_positive_reinstatement(low)
    cancels_warning = bool(
        re.search(r"hủ[yỳ]\s+bỏ\s+(?:cảnh\s+báo|kiểm\s+soát|hạn\s+chế|diện)", low)
    )
    for subtype, risk_level, severity, keywords in _REGULATORY_RULE_ORDER:
        if subtype == "warning" and (has_reinstatement or cancels_warning):
            continue
        if any(k in low for k in keywords):
            return RegulatoryClassification(
                subtype=subtype,  # type: ignore[arg-type]
                risk_level=risk_level,
                severity=severity,
            )
    return RegulatoryClassification(subtype="other", risk_level=0, severity="info")


_INSIDER_FALSE_CONTEXT_RE = re.compile(
    r"chào\s+bán|mở\s+bán|bán\s+hàng|bán\s+lẻ|bán\s+buôn|giá\s+bán|doanh\s+số\s+bán|"
    r"quyền\s+mua|mua\s+lại\s+cổ\s+phiếu\s+quỹ|sức\s+mua",
    re.IGNORECASE,
)


def classify_insider_trade(title: str, detail: str = "") -> InsiderClassification:
    """Classify insider / major shareholder trade headlines."""
    text = f"{title} {detail}".strip()
    cleaned = _INSIDER_FALSE_CONTEXT_RE.sub(" ", text)
    low = cleaned.lower()

    if any(k in low for k in ("muốn bán", "đăng ký bán", "thoái", "disposed")) or re.search(
        r"\b(?:bán|sell)\b", low
    ):
        direction: Literal["buy", "sell", "transfer", "unknown"] = "sell"
    elif any(k in low for k in ("muốn mua", "đăng ký mua", "acquired")) or re.search(
        r"\b(?:mua|gom|buy)\b", low
    ):
        direction = "buy"
    elif any(k in low for k in ("chuyển nhượng", "transfer")):
        direction = "transfer"
    else:
        direction = "unknown"

    full_low = text.lower()
    if any(
        k in full_low
        for k in (
            "bất thành",
            "không thành công",
            "hủy đăng ký",
            "huỷ đăng ký",
            "không bán được",
            "không mua được",
            "đã bán 0 ",
            "đã mua 0 ",
        )
    ):
        status: Literal["executed", "planned", "cancelled", "unknown"] = "cancelled"
    elif any(k in low for k in ("đã bán", "đã mua", "đã thoái", "kết quả giao dịch", "completed")):
        status = "executed"
    elif any(k in low for k in ("đăng ký", "dự kiến", "plans to")) or re.search(r"\bmuốn\b", low):
        status = "planned"
    else:
        status = "unknown"

    shares: float | None = None
    if status != "cancelled" and direction != "unknown":
        m_sh = _SHARES_COUNT_RE.search(text)
        if m_sh:
            shares = _parse_locale_number(m_sh.group(1))
        else:
            m_mil = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:triệu|m)\s*(?:cp|cổ\s+phiếu|shares?)", low)
            if m_mil:
                num = _parse_locale_number(m_mil.group(1))
                if num is not None:
                    shares = num * 1_000_000.0
    return InsiderClassification(direction=direction, execution_status=status, shares_count=shares)


def classify_news_scope_and_category(
    title: str,
    snippet: str,
    *,
    symbols: Sequence[str],
    category_hint: NewsCategory = "general_finance",
    scope_hint: EventScope = "general",
) -> tuple[EventScope, NewsCategory, SeverityLevel]:
    """Refine news scope, category, and severity from title/snippet semantics."""
    low = f"{title} {snippet}".lower()
    category = category_hint
    severity: SeverityLevel = "info"

    is_listing_phrase = any(
        k in low
        for k in (
            "hủy niêm yết",
            "huỷ niêm yết",
            "hủy bỏ niêm yết",
            "chấm dứt niêm yết",
            "ra khỏi diện",
            "diện cảnh báo",
            "diện kiểm soát",
            "hạn chế giao dịch",
            "tạm ngừng giao dịch",
            "giao dịch trở lại",
            "niêm yết bổ sung",
            "chào sàn",
            "delisting",
            "trading halt",
        )
    )
    has_regulator_enforcement = any(
        k in low
        for k in (
            "ubcknn",
            "xử phạt",
            "phạt tiền",
            "vi phạm hành chính",
            "thu hồi",
            "sec enforcement",
            "fined",
        )
    )
    reg = classify_regulatory_action(title, snippet)
    if reg.risk_level >= 3 or (
        reg.risk_level >= 2
        and (
            has_regulator_enforcement
            or (not is_listing_phrase and category_hint != "listing_status")
        )
    ):
        category = "regulatory"
        severity = reg.severity
    elif category_hint == "listing_status" or is_listing_phrase:
        listing = classify_listing_event(title, snippet)
        category = "listing_status"
        severity = listing.severity
    elif any(
        k in low
        for k in (
            "cổ tức",
            "thưởng cổ phiếu",
            "cổ phiếu thưởng",
            "chốt quyền",
            "gdkhq",
            "phát hành",
            "đhđcđ",
        )
    ):
        category = "corporate_action"
        severity = "medium"
    elif any(k in low for k in ("nội bộ", "cổ đông lớn", "thoái", "hđqt", "tổng giám đốc")):
        if classify_insider_trade(title, snippet).direction != "unknown":
            category = "insider_trade"
            severity = "medium"
    elif category_hint == "general_finance" and is_market_macro_text(low):
        category = (
            "macro_policy"
            if any(k in low for k in ("lãi suất", "chính sách", "sbv", "fed"))
            else "market_flow"
        )

    if symbols:
        scope: EventScope = "ticker"
    elif category == "macro_policy" or scope_hint == "policy":
        scope = "policy"
    elif is_market_macro_text(low) or scope_hint == "market":
        scope = "market"
    else:
        scope = scope_hint
    return scope, category, severity


# --------------------------------------------------------------------------------------
# Share-changing factor math & cross-source discrepancy explanation
# --------------------------------------------------------------------------------------


def _strip_clock_timestamps(text: str) -> str:
    cleaned = _DATE_TIME_COLON_RE.sub(r"\1 ", text or "")
    for pat in _CLOCK_STRIP_PATTERNS:
        cleaned = pat.sub(" ", cleaned)
    return cleaned


def _iter_colon_ratios(cleaned: str) -> list[tuple[str, float, float]]:
    out: list[tuple[str, float, float]] = []
    for m in _RATIO_COLON_RE.finditer(cleaned):
        raw_tok = m.group(0)
        before = cleaned[max(0, m.start() - 24) : m.start()]
        after = cleaned[m.end() : min(len(cleaned), m.end() + 24)]
        has_colon_space = bool(re.search(r":\s", raw_tok))
        if _PROSE_COLON_BEFORE_RE.search(before):
            continue
        if has_colon_space and _PROSE_COLON_AFTER_RE.match(after):
            continue
        a = _parse_locale_number(m.group(1))
        b = _parse_locale_number(m.group(2))
        if a is None or b is None or a <= 0 or b <= 0:
            continue
        if has_colon_space and a >= 2000 and a == int(a):
            continue
        out.append((raw_tok.replace(" ", ""), a, b))
    return out


def _extract_ratio_tokens(text: str) -> list[str]:
    cleaned = _strip_clock_timestamps(text)
    return [tok for tok, _a, _b in _iter_colon_ratios(cleaned)]


def parse_share_factor(text: str) -> float | None:
    """Compute multiplicative share-count factor from splits, stock dividends, or rights.

    - Forward splits (``2:1``, ``1 thành 2``, ``10-for-1``, ``chia tách 1:5``) -> ``factor > 1.0``.
    - Reverse splits (``gộp cổ phiếu 10:1``, ``Gộp 10 cổ phiếu thành 1``) -> ``factor < 1.0``.
    - Stock dividend / bonus / rights ``a:b`` -> compound ``(a + b) / a`` after stripping
      clock timestamps (``14:30``, ``08:30 ngày``, ``(08:05)``) and prose colons.
    - Stock dividend / bonus percentage (e.g. ``cổ tức bằng cổ phiếu 15%``) -> ``1.15``.
    """
    src = _WITHOUT_PER_SHARE_UNIT_RE.sub("/cp", text or "")
    if not _SHARE_CHANGING_RE.search(src):
        return None
    cleaned = _strip_clock_timestamps(src)
    is_rev_split = bool(_REVERSE_SPLIT_RE.search(cleaned))
    is_fwd_split = not is_rev_split and bool(_FORWARD_SPLIT_RE.search(cleaned))

    factor = 1.0
    matched = False
    for _tok, a, b in _iter_colon_ratios(cleaned):
        if is_rev_split:
            factor *= min(a, b) / max(a, b)
        elif is_fwd_split:
            factor *= max(a, b) / min(a, b)
        else:
            factor *= (a + b) / a
        matched = True

    if not matched:
        for m_for in _FOR_SPLIT_RE.finditer(cleaned):
            first = _parse_locale_number(m_for.group(1))
            op = (m_for.group(2) or "").lower()
            second = _parse_locale_number(m_for.group(3))
            if first is None or second is None or first <= 0 or second <= 0:
                continue
            if op == "-for-":
                ratio_val = first / second
                factor *= min(ratio_val, 1.0 / ratio_val) if is_rev_split else ratio_val
            else:
                ratio_val = second / first
                if is_rev_split:
                    factor *= min(first, second) / max(first, second)
                elif is_fwd_split:
                    factor *= max(first, second) / min(first, second)
                else:
                    factor *= ratio_val
            matched = True

    if not matched and not is_rev_split:
        clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(cleaned) if c and c.strip()]
        for clause in clauses or [cleaned]:
            pct_matches = list(_STOCK_PCT_RE.finditer(clause))
            if not pct_matches:
                pct_matches = list(_STOCK_PCT_PRE_RE.finditer(clause))
            for m_pct in pct_matches:
                pct = _parse_locale_number(m_pct.group(1))
                if pct is not None and 0 < pct <= 10000:
                    factor *= 1.0 + pct / 100.0
                    matched = True

    if not matched or not math.isfinite(factor) or factor <= 0.0 or abs(factor - 1.0) < 1e-6:
        return None
    return round(factor, 4)


def explain_share_ratio(
    events: Sequence[Mapping[str, Any] | CorporateEventRecord],
    observed_ratio: float,
    *,
    tolerance: float = 0.08,
) -> ShareRatioExplanation:
    """Match an observed cross-source share ratio against per-symbol corporate actions.

    Deduplicates duplicate actions across providers (same symbol + event_date + factor),
    never combines actions from different symbols, sorts chronologically, and returns the
    minimum-residual single action or bounded subset chain within ``tolerance``.
    """
    if not math.isfinite(observed_ratio) or observed_ratio <= 0:
        return ShareRatioExplanation(
            explained=False,
            observed_ratio=0.0,
            explanation="invalid non-positive share ratio",
        )
    if abs(observed_ratio - 1.0) <= 1e-6:
        return ShareRatioExplanation(
            explained=True,
            observed_ratio=1.0,
            matched_factor=1.0,
            residual_error_pct=0.0,
            explanation="share counts agree within rounding noise (ratio ~ 1.0x)",
        )

    def _ev_dict(item: Mapping[str, Any] | CorporateEventRecord) -> dict[str, Any]:
        if isinstance(item, CorporateEventRecord):
            return {
                "event_id": item.event_id,
                "symbol": item.symbol,
                "event_date": item.ex_date or item.event_date,
                "title": item.title,
                "share_factor": item.share_factor,
                "is_share_changing": item.is_share_changing,
            }
        return dict(item)

    raw_changing = [
        d
        for d in (_ev_dict(ev) for ev in events)
        if d.get("is_share_changing")
        and isinstance(d.get("share_factor"), (int, float))
        and math.isfinite(float(d["share_factor"]))
        and float(d["share_factor"]) > 0.0
        and abs(float(d["share_factor"]) - 1.0) > 1e-6
    ]
    if not raw_changing:
        return ShareRatioExplanation(
            explained=False,
            observed_ratio=round(observed_ratio, 4),
            explanation="no share-changing corporate actions available for comparison",
        )

    # Deduplicate same corporate action reported by multiple providers (CafeF + CoTuc + RSS),
    # including T-1/T date offsets within <= 10 days for the same symbol & share_factor.
    ordered_raw = sorted(
        raw_changing,
        key=lambda e: 0 if _parse_text_date(e.get("event_date")) else 1,
    )
    deduped_changing: list[dict[str, Any]] = []
    kept_by_sym_factor: dict[tuple[str, float], list[tuple[int, str]]] = {}
    for ev in ordered_raw:
        sym_key = str(ev.get("symbol") or "").strip().upper()
        date_key = _parse_text_date(ev.get("event_date")) or ""
        f_key = round(float(ev["share_factor"]), 4)
        ord_d = _safe_date_ordinal(date_key)
        prior_list = kept_by_sym_factor.get((sym_key, f_key), [])
        is_dup = False
        for p_ord, p_date in prior_list:
            if (
                (ord_d > 0 and p_ord > 0 and abs(ord_d - p_ord) <= 10)
                or (not date_key and not p_date)
                or (ord_d == 0 and prior_list)
            ):
                is_dup = True
                break
        if is_dup:
            continue
        kept_by_sym_factor.setdefault((sym_key, f_key), []).append((ord_d, date_key))
        deduped_changing.append(ev)

    # Partition by symbol so actions of distinct companies are never multiplied together.
    by_sym: dict[str, list[dict[str, Any]]] = {}
    for ev in deduped_changing:
        by_sym.setdefault(str(ev.get("symbol") or "").strip().upper(), []).append(ev)

    # Support both direct orientation (observed_ratio) and inverted (1/observed_ratio if <1 vs >1).
    targets = [observed_ratio]
    if observed_ratio != 0:
        inv = 1.0 / observed_ratio
        if abs(inv - observed_ratio) > 1e-6:
            targets.append(inv)

    best_match: tuple[float, int, float, float, list[dict[str, Any]]] | None = None
    for sym_norm, sym_events in by_sym.items():
        ordered = sorted(
            sym_events,
            key=lambda e: str(e.get("event_date") or "0000-00-00"),
        )
        # Bound combinatorics over the 8 most recent dated events per symbol;
        # never chain unattributed actions (sym == "").
        window = ordered[-8:]
        max_len = 1 if not sym_norm else min(len(window), 5)
        for length in range(1, max_len + 1):
            for subset in combinations(window, length):
                prod = 1.0
                for item in subset:
                    prod *= float(item["share_factor"])
                for tgt in targets:
                    err = abs(prod - tgt) / tgt
                    if err <= tolerance:
                        cand = (err, length, tgt, prod, list(subset))
                        if best_match is None or (cand[0], cand[1]) < (
                            best_match[0],
                            best_match[1],
                        ):
                            best_match = cand

    if best_match is not None:
        err, length, tgt, prod, chain = best_match
        desc = (
            f"explained by single action ({round(prod, 4)}x): {chain[0].get('title', '')}"
            if length == 1
            else f"explained by {length} chained actions (cumulative {prod:.4f}x)"
        )
        if abs(tgt - observed_ratio) > 1e-6:
            desc = f"{desc} (matched inverse ratio 1/{round(observed_ratio, 4)})"
        return ShareRatioExplanation(
            explained=True,
            observed_ratio=round(observed_ratio, 4),
            matched_factor=round(prod, 4),
            residual_error_pct=round(err * 100.0, 2),
            matched_events=chain,
            explanation=desc,
        )

    return ShareRatioExplanation(
        explained=False,
        observed_ratio=round(observed_ratio, 4),
        explanation="no single or cumulative corporate action sequence matched within tolerance",
    )


# --------------------------------------------------------------------------------------
# Deduplication & dual-semantics freshness filtering
# --------------------------------------------------------------------------------------


_SEV_PRIORITY = {"critical": 0, "high": 1, "medium": 2, "info": 3}


def _to_vn_date(dt: datetime) -> date:
    norm = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    try:
        return norm.astimezone(_VN_TZ).date()
    except OverflowError:
        utc_dt = norm.astimezone(UTC)
        return date.max if utc_dt.year >= 9999 else date.min


def _safe_dt_timestamp(dt: datetime | None) -> float:
    if dt is None:
        return 0.0
    norm = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    try:
        return norm.timestamp()
    except (OSError, OverflowError, ValueError):
        return (norm.replace(tzinfo=None) - datetime(1970, 1, 1)).total_seconds()


def deduplicate_news_items(items: Sequence[ParsedNewsItem]) -> list[ParsedNewsItem]:
    """Collapse syndicated duplicates across canonical URL and content fingerprint."""
    by_key: dict[str, ParsedNewsItem] = {}
    url_to_key: dict[str, str] = {}
    untagged_fp_to_key: dict[str, str] = {}
    single_sym_fp_to_key: dict[str, str | None] = {}
    sym_fp_to_key: dict[tuple[str, str], str] = {}
    ordered_keys: list[str] = []

    for item in items:
        c_url = canonical_url(item.url)
        raw_fp = item.fingerprint or content_fingerprint(item.title)
        item.fingerprint = raw_fp
        sym_tag = item.symbols[0] if len(item.symbols) == 1 else ""
        dedup_key = f"{sym_tag}:{raw_fp}" if sym_tag and len(item.title) < 60 else raw_fp

        existing_key: str | None = None
        if c_url and c_url in url_to_key:
            existing_key = url_to_key[c_url]
        elif dedup_key in by_key:
            existing_key = dedup_key
        elif len(item.title) >= 25:
            if not item.symbols:
                cand_k = single_sym_fp_to_key.get(raw_fp)
                if cand_k and cand_k in by_key:
                    existing_key = cand_k
            elif sym_tag:
                alias_k = sym_fp_to_key.get((sym_tag, raw_fp))
                if alias_k and alias_k in by_key:
                    existing_key = alias_k
                elif raw_fp in untagged_fp_to_key:
                    cand_k = untagged_fp_to_key.pop(raw_fp)
                    if cand_k in by_key:
                        existing_key = cand_k

        if existing_key is not None:
            prev = by_key[existing_key]
            prev.duplicate_count += 1
            if c_url and c_url not in url_to_key:
                url_to_key[c_url] = existing_key
            if item.source_id != prev.source_id and item.source_id not in prev.also_reported_by:
                prev.also_reported_by.append(item.source_id)
            for sym in item.symbols:
                if sym not in prev.symbols:
                    prev.symbols.append(sym)
            if len(item.title) >= 25 and len(prev.symbols) == 1:
                only_sym = prev.symbols[0]
                sym_fp_to_key.setdefault((only_sym, raw_fp), existing_key)
                if raw_fp not in single_sym_fp_to_key:
                    single_sym_fp_to_key[raw_fp] = existing_key
                elif single_sym_fp_to_key[raw_fp] != existing_key:
                    single_sym_fp_to_key[raw_fp] = None
            elif len(prev.symbols) > 1:
                single_sym_fp_to_key[raw_fp] = None
            if prev.symbols and prev.scope in ("general", "market", "policy", "sector"):
                prev.scope = "ticker"
            elif (
                not prev.symbols
                and prev.scope == "general"
                and item.scope in ("policy", "market", "sector")
            ):
                prev.scope = item.scope
            if prev.category == "general_finance" and item.category != "general_finance":
                prev.category = item.category
            if _SEV_PRIORITY.get(item.severity, 3) < _SEV_PRIORITY.get(prev.severity, 3):
                prev.severity = item.severity
            if len(item.snippet) > len(prev.snippet):
                prev.snippet = item.snippet
            if prev.published_at is None and item.published_at is not None:
                prev.published_at = item.published_at
                if prev.event_date is None:
                    prev.date_status = "published_at"
            if prev.event_date is None and item.event_date is not None:
                prev.event_date = item.event_date
                prev.date_status = "event_date"
            continue

        by_key[dedup_key] = item
        if c_url:
            url_to_key[c_url] = dedup_key
        if len(item.title) >= 25:
            if not item.symbols:
                untagged_fp_to_key.setdefault(raw_fp, dedup_key)
            elif sym_tag:
                sym_fp_to_key.setdefault((sym_tag, raw_fp), dedup_key)
                if raw_fp not in single_sym_fp_to_key:
                    single_sym_fp_to_key[raw_fp] = dedup_key
                elif single_sym_fp_to_key[raw_fp] != dedup_key:
                    single_sym_fp_to_key[raw_fp] = None
        ordered_keys.append(dedup_key)

    return [by_key[k] for k in ordered_keys]


def filter_news_freshness(
    items: Sequence[ParsedNewsItem],
    *,
    today: date | None = None,
    max_age_days: int = 7,
) -> list[ParsedNewsItem]:
    """Filter news using dual validity semantics."""
    ref_day = today or datetime.now(_VN_TZ).date()
    out: list[ParsedNewsItem] = []
    for item in items:
        if item.event_date:
            try:
                ev_day = date.fromisoformat(item.event_date)
                item.date_status = "event_date"
                if ev_day >= ref_day:
                    out.append(item)
                continue
            except ValueError:
                pass
        if item.published_at is not None:
            item.date_status = "published_at"
            pub_day = _to_vn_date(item.published_at)
            if (ref_day - pub_day).days <= max(0, max_age_days):
                out.append(item)
            continue
        item.date_status = "undated"
        out.append(item)
    return out


# --------------------------------------------------------------------------------------
# Safe RSS / HTML / JSON parsers (no Playwright, no lxml B410, defusedxml only)
# --------------------------------------------------------------------------------------


_MAX_HTML_ANCHORS = 1000
_MAX_ANCHOR_CHARS = 512
_MAX_FEED_ITEMS = 250
_BLOCK_HTML_TAGS = frozenset(
    {"br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "section", "td", "th", "tr"}
)
_IGNORED_HTML_TAGS = frozenset({"script", "style", "noscript", "svg", "template"})


class _AnchorExtractor(HTMLParser):
    """Bounded stdlib HTML anchor extractor preserving block vs inline text spacing."""

    def __init__(self, *, split_inline_spans: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self._split_inline_spans = split_inline_spans
        self.anchors: list[tuple[str, list[str], bool]] = []
        self._in_a = False
        self._a_depth = 0
        self._href = ""
        self._tokens: list[str] = []
        self._cur_buf: list[str] = []
        self._total_chars = 0
        self._has_img = False
        self._in_ignored = 0

    def _flush_segment(self) -> None:
        if not self._cur_buf:
            return
        seg = re.sub(r"\s+", " ", "".join(self._cur_buf)).strip()
        self._cur_buf.clear()
        if seg and len(self._tokens) < 64:
            self._tokens.append(seg)

    def _flush_anchor(self) -> None:
        if not self._in_a:
            return
        self._flush_segment()
        if self._href and len(self.anchors) < _MAX_HTML_ANCHORS:
            self.anchors.append((self._href, list(self._tokens), self._has_img))
        self._in_a = False
        self._a_depth = 0
        self._href = ""
        self._tokens = []
        self._cur_buf = []
        self._total_chars = 0
        self._has_img = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in _IGNORED_HTML_TAGS:
            self._in_ignored += 1
            return
        if self._in_ignored > 0:
            return
        if t == "a":
            href = ""
            for k, v in attrs:
                if k.lower() == "href" and v:
                    href = v.strip()
                    break
            if self._in_a and not href:
                self._a_depth += 1
                return
            if self._in_a:
                self._flush_anchor()
            self._in_a = True
            self._a_depth = 1
            self._href = href
            self._tokens = []
            self._cur_buf = []
            self._total_chars = 0
            self._has_img = False
        elif self._in_a:
            if t == "img":
                self._has_img = True
            elif t in _BLOCK_HTML_TAGS or (self._split_inline_spans and t == "span"):
                self._flush_segment()

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _IGNORED_HTML_TAGS:
            self._in_ignored = max(0, self._in_ignored - 1)
            return
        if self._in_ignored > 0:
            return
        if t == "a" and self._in_a:
            self._a_depth = max(0, self._a_depth - 1)
            if self._a_depth == 0:
                self._flush_anchor()
        elif self._in_a and (t in _BLOCK_HTML_TAGS or (self._split_inline_spans and t == "span")):
            self._flush_segment()

    def handle_data(self, data: str) -> None:
        if self._in_ignored > 0 or not self._in_a or self._total_chars >= _MAX_ANCHOR_CHARS:
            return
        collapsed = re.sub(r"\s+", " ", data)
        if not collapsed:
            return
        if collapsed == " ":
            if self._cur_buf and not self._cur_buf[-1].endswith(" "):
                self._cur_buf.append(" ")
            return
        rem = _MAX_ANCHOR_CHARS - self._total_chars
        chunk = collapsed[:rem]
        if chunk:
            self._cur_buf.append(chunk)
            non_ws = len(chunk.strip())
            if non_ws > 0:
                self._total_chars += non_ws


_KNOWN_HTML_TAG_NAMES = (
    "br|hr|p|b|i|u|s|em|strong|small|sub|sup|mark|del|ins|div|span|ul|ol|li|dl|dt|dd|"
    "h[1-6]|tr|td|th|thead|tbody|tfoot|table|a|img|figure|figcaption|blockquote|"
    "pre|code|font|center|section|article|header|footer|aside|main|nav|time|video|audio"
)
# Linear-time patterns without nested quantifiers (immune to ReDoS on unclosed '<a ...').
_TAG_OR_COMMENT_RE = re.compile(
    rf"<!--[\s\S]{{0,2000}}?-->"
    rf"|</[a-zA-Z][a-zA-Z0-9:-]{{0,30}}(?:\s[^<>]{{0,4096}})?>"
    rf"|<(?:/?(?:{_KNOWN_HTML_TAG_NAMES})\b(?:/>|>|\s[^<>]{{0,4096}}>))"
    rf"|<[a-zA-Z][a-zA-Z0-9:-]{{0,30}}(?:/>|\s[^<>=]{{0,512}}=[^<>]{{0,4096}}>)",
    re.IGNORECASE,
)


def _strip_script_style_blocks(raw: str) -> str:
    low = raw.lower()
    out: list[str] = []
    pos = 0
    n = len(raw)
    while pos < n:
        s_idx = low.find("<script", pos)
        c_idx = low.find("<style", pos)
        if s_idx == -1 and c_idx == -1:
            out.append(raw[pos:])
            break
        if s_idx == -1 or (c_idx != -1 and c_idx < s_idx):
            idx, close_tag = c_idx, "</style"
        else:
            idx, close_tag = s_idx, "</script"
        after_name = idx + (len("<style") if close_tag == "</style" else len("<script"))
        if after_name < n and (raw[after_name].isalnum() or raw[after_name] in ("-", "_")):
            out.append(raw[pos:after_name])
            pos = after_name
            continue
        out.append(raw[pos:idx])
        gt_open = raw.find(">", after_name)
        if gt_open == -1:
            break
        close_start = low.find(close_tag, gt_open + 1)
        if close_start == -1:
            break
        close_gt = raw.find(">", close_start + len(close_tag))
        if close_gt == -1:
            break
        out.append(" ")
        pos = close_gt + 1
    return "".join(out)


def _strip_tags(raw: str) -> str:
    text = _html_unescape((raw or "")[:32768])
    text = _strip_script_style_blocks(text)
    no_tags = _TAG_OR_COMMENT_RE.sub(" ", text)
    return re.sub(r"\s+", " ", no_tags).strip()


def _parse_rss_datetime(raw: str | None) -> datetime | None:
    s = (raw or "").strip()
    if not s:
        return None
    # Handle ISO-8601 strings first so cache round-trips and Atom feeds keep exact UTC timestamps.
    iso_cand = f"{s[:-1]}+00:00" if s.endswith(("Z", "z")) else s
    with contextlib.suppress(ValueError, OverflowError, TypeError):
        dt = datetime.fromisoformat(iso_cand)
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        pass
    for fmt in ("%a, %d %b %y %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        except (ValueError, OverflowError):
            continue
    return None


def _element_text(el: Any, *, max_depth: int = 64, max_chars: int = 16_384) -> str:
    """Iterative depth- and char-bounded XML text extraction (safe against deep XHTML)."""
    buf: list[str] = []
    total_chars = 0
    # Stack frames: (node, depth, phase) where phase 0=enter, 1=exit-block-tail
    stack: list[tuple[Any, int, int]] = [(el, 0, 0)]
    while stack and total_chars < max_chars:
        node, depth, phase = stack.pop()
        if phase == 1:
            sub_tag = node.tag.rsplit("}", 1)[-1].lower() if isinstance(node.tag, str) else ""
            if sub_tag in _BLOCK_HTML_TAGS or sub_tag == "br":
                buf.append(" ")
                total_chars += 1
            if node.tail:
                tail = node.tail[: max_chars - total_chars]
                if tail:
                    buf.append(tail)
                    total_chars += len(tail)
            continue

        if depth > 0:
            sub_tag = node.tag.rsplit("}", 1)[-1].lower() if isinstance(node.tag, str) else ""
            if sub_tag in _BLOCK_HTML_TAGS or sub_tag == "br":
                buf.append(" ")
                total_chars += 1
            stack.append((node, depth, 1))

        if node.text:
            chunk = node.text[: max_chars - total_chars]
            if chunk:
                buf.append(chunk)
                total_chars += len(chunk)

        if depth < max_depth:
            children = list(node)
            for sub in reversed(children):
                stack.append((sub, depth + 1, 0))
    return "".join(buf)


def _atom_child_text(node: Any, *local_names: str) -> str:
    for child in node:
        tag = child.tag.rsplit("}", 1)[-1] if isinstance(child.tag, str) else ""
        if tag in local_names:
            text = _element_text(child)
            if text.strip():
                return text
    return ""


def parse_rss_feed_xml(
    xml_text: str,
    *,
    source_id: str,
    category_hint: NewsCategory = "general_finance",
    scope_hint: EventScope = "general",
    allowed_symbols: set[str] | frozenset[str] | None = None,
    raise_on_error: bool = False,
    today: date | None = None,
) -> list[ParsedNewsItem]:
    """Parse RSS 2.0, RDF/RSS 1.0, or Atom XML feeds safely via ``defusedxml.ElementTree``."""
    raw = (xml_text or "").lstrip("\ufeff \t\r\n")
    if not raw:
        return []
    try:
        root = _xml_fromstring(raw)
    except (XMLParseError, DefusedXmlException):
        if raise_on_error:
            raise
        return []

    items: list[ParsedNewsItem] = []
    ref_day = today or datetime.now(_VN_TZ).date()
    today_iso = ref_day.isoformat()
    root_local = root.tag.rsplit("}", 1)[-1] if isinstance(root.tag, str) else ""
    if root_local == "feed":
        rss_nodes = [
            el for el in root if isinstance(el.tag, str) and el.tag.rsplit("}", 1)[-1] == "entry"
        ]
    else:
        rss_nodes = root.findall("./channel/item")
    if not rss_nodes:
        rss_nodes = [
            el
            for el in root.iter()
            if isinstance(el.tag, str) and el.tag.rsplit("}", 1)[-1] in ("item", "entry")
        ]
    for node in rss_nodes[:_MAX_FEED_ITEMS]:
        is_atom = isinstance(node.tag, str) and node.tag.rsplit("}", 1)[-1] == "entry"
        if is_atom:
            title = _strip_tags(_atom_child_text(node, "title")[:4096])
            link = None
            for child in node:
                if isinstance(child.tag, str) and child.tag.rsplit("}", 1)[-1] == "link":
                    rel = (child.attrib.get("rel") or "alternate").lower()
                    if rel in ("alternate", ""):
                        link = _safe_http_url(child.attrib.get("href") or child.text or "")
                        if link:
                            break
            desc = _strip_tags(_atom_child_text(node, "summary", "content")[:8192])
            pub_dt = _parse_rss_datetime(_atom_child_text(node, "published", "updated"))
        else:
            raw_title = node.findtext("title") or _atom_child_text(node, "title")
            title = _strip_tags(raw_title[:4096])
            link = _safe_http_url(node.findtext("link") or _atom_child_text(node, "link"))
            if not link:
                for child in node:
                    if isinstance(child.tag, str) and child.tag.rsplit("}", 1)[-1] == "link":
                        rel = (child.attrib.get("rel") or "alternate").lower()
                        if rel in ("alternate", ""):
                            link = _safe_http_url(child.attrib.get("href") or "")
                            if link:
                                break
            if not link:
                guid_el = node.find("guid")
                if (
                    guid_el is not None
                    and guid_el.attrib.get("isPermaLink", "true").lower() != "false"
                ):
                    link = _safe_http_url(guid_el.text or "")
            raw_desc = node.findtext("description") or _atom_child_text(
                node, "description", "encoded", "summary"
            )
            desc = _strip_tags(raw_desc[:8192])
            pub_dt = _parse_rss_datetime(
                node.findtext("pubDate")
                or _atom_child_text(node, "pubDate", "date", "published", "updated")
            )

        if not title:
            continue
        syms = extract_tickers_from_text(f"{title} {desc[:200]}", allowed_symbols=allowed_symbols)
        scope, category, severity = classify_news_scope_and_category(
            title,
            desc,
            symbols=syms,
            category_hint=category_hint,
            scope_hint=scope_hint,
        )
        # Only bind future/current event_date so past dates cited in fresh articles don't drop them.
        parsed_title_d = (
            _parse_text_date(title) if category in ("listing_status", "corporate_action") else None
        )
        ev_date = parsed_title_d if (parsed_title_d and parsed_title_d >= today_iso) else None
        items.append(
            ParsedNewsItem(
                title=title,
                url=link,
                source_id=source_id,
                snippet=desc[:500],
                published_at=pub_dt,
                event_date=ev_date,
                symbols=syms,
                scope=scope,
                category=category,
                severity=severity,
                stable_id=stable_document_id(link, source_id=source_id, fallback_text=title),
                fingerprint=content_fingerprint(title),
            )
        )
    return items


def parse_html_article_links(
    html_text: str,
    *,
    source_id: str,
    base_url: str,
    category_hint: NewsCategory = "general_finance",
    scope_hint: EventScope = "general",
    min_title_len: int = 30,
    allowed_symbols: set[str] | frozenset[str] | None = None,
) -> list[ParsedNewsItem]:
    """Extract non-image article anchors >= ``min_title_len`` chars from HTML."""
    parser = _AnchorExtractor()
    parser.feed(html_text or "")
    parser.close()

    items: list[ParsedNewsItem] = []
    seen_urls: set[str] = set()
    for href, tokens, has_img in parser.anchors:
        if has_img or not href or href.startswith("#"):
            continue
        title = " ".join(tokens).strip()
        if len(title) < min_title_len:
            continue
        try:
            abs_url = urljoin(base_url, href)
        except ValueError:
            continue
        safe_url = _safe_http_url(abs_url)
        if not safe_url:
            continue
        path_low = urlsplit(safe_url).path.lower().rstrip("/")
        if not path_low or path_low in ("/tim-kiem", "/login", "/register", "/logout"):
            continue
        if "⌘k" in title.lower():
            continue
        c_url = canonical_url(safe_url)
        if c_url in seen_urls:
            continue
        seen_urls.add(c_url)
        syms = extract_tickers_from_text(title, allowed_symbols=allowed_symbols)
        scope, category, severity = classify_news_scope_and_category(
            title,
            "",
            symbols=syms,
            category_hint=category_hint,
            scope_hint=scope_hint,
        )
        items.append(
            ParsedNewsItem(
                title=title,
                url=safe_url,
                source_id=source_id,
                snippet="",
                published_at=None,
                event_date=None,
                symbols=syms,
                scope=scope,
                category=category,
                severity=severity,
                stable_id=stable_document_id(safe_url, source_id=source_id, fallback_text=title),
                fingerprint=content_fingerprint(title),
            )
        )
        if len(items) >= _MAX_FEED_ITEMS:
            break
    return items


def _extract_cash_dividend(
    text: str,
    *,
    has_stock: bool,
) -> tuple[float | None, float | None]:
    """Extract cash dividend % and VND/share per clause without crossing into stock/par clauses."""
    cash_pct: float | None = None
    cash_vnd: float | None = None
    norm_text = _WITHOUT_PER_SHARE_UNIT_RE.sub("/cp", text or "")
    clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(norm_text) if c and c.strip()]
    for clause in clauses or [norm_text]:
        low_c = clause.lower()
        has_cash_kw = any(k in low_c for k in ("tiền", "cash", "tạm ứng cổ tức"))
        clause_has_stock = bool(_SHARE_CHANGING_RE.search(clause)) or any(
            k in low_c
            for k in ("cổ phiếu", "thưởng", "tăng vốn", "phát hành", "bổ sung", "quyền mua")
        )
        m_pre_cash = re.search(
            r"(\d+(?:[.,]\d+)?)\s*%\s*(?:bằng\s+tiền(?:\s+mặt)?|tiền\s*mặt|cash)\b",
            low_c,
        )
        if not has_cash_kw and (has_stock or clause_has_stock) and not m_pre_cash:
            continue
        if cash_vnd is None and (has_cash_kw or (not has_stock and not clause_has_stock)):
            for m_vnd in _CASH_VND_RE.finditer(clause):
                raw_prefix = low_c[max(0, m_vnd.start() - 64) : m_vnd.start()]
                sep_pos = max(raw_prefix.rfind(","), raw_prefix.rfind(";"), raw_prefix.rfind("("))
                sub_prefix = raw_prefix[sep_pos + 1 :] if sep_pos != -1 else raw_prefix
                if _NON_PAYOUT_VND_CONTEXT_RE.search(sub_prefix):
                    continue
                val = _parse_locale_number(m_vnd.group(1))
                if val is not None and 0.0 < val <= 1_000_000.0:
                    cash_vnd = val
                    break
        if cash_pct is None and (has_cash_kw or not has_stock or m_pre_cash):
            clean_pct_clause = _TAX_PCT_PHRASE_RE.sub(" ", clause)
            clean_pct_low = clean_pct_clause.lower()
            m_cash = re.search(
                r"(\d+(?:[.,]\d+)?)\s*%\s*(?:bằng\s+tiền(?:\s+mặt)?|tiền\s*mặt|cash)\b",
                clean_pct_low,
            ) or re.search(
                r"(?:bằng\s+tiền(?:\s+mặt)?|tiền\s*mặt|cash)[^;\n,/]{0,50}?\b(\d+(?:[.,]\d+)?)\s*%",
                clean_pct_low,
            )
            if m_cash is None and not clause_has_stock and (has_cash_kw or not has_stock):
                m_cash = _CASH_PCT_RE.search(clean_pct_clause)
            if m_cash:
                cand_pct = _parse_locale_number(m_cash.group(1))
                if cand_pct is not None and 0.0 <= cand_pct <= 10_000.0:
                    cash_pct = cand_pct
    return cash_pct, cash_vnd


def parse_cafef_corporate_actions(
    payload: Any,
    symbol: str,
) -> list[CorporateEventRecord]:
    """Parse CafeF ``LichSuKien.ashx`` JSON payload into structured events."""
    sym = ensure_symbol(symbol) or symbol.strip().upper()
    if not sym or not isinstance(payload, dict):
        return []
    rows = payload.get("Data")
    if not isinstance(rows, list):
        return []
    vn_meta = load_vn_tickers().get(sym) or {}
    exchange = str(vn_meta.get("exchange") or "").strip() or None

    records: list[CorporateEventRecord] = []
    seen_ids: set[str] = set()
    for idx, row in enumerate(rows[:_MAX_FEED_ITEMS]):
        if not isinstance(row, dict):
            continue
        raw_text = row.get("Text")
        text = (
            " ".join(str(x) for x in raw_text if x is not None)
            if isinstance(raw_text, list)
            else str(raw_text or "")
        )
        text = re.sub(r"\s+", " ", _html_unescape(text)).strip()
        if not text:
            continue
        ev_date = (
            _parse_ms_json_date(row.get("Time"))
            or _parse_text_date(row.get("Time"))
            or _parse_text_date(text)
        )
        factor = parse_share_factor(text)
        ratios = _extract_ratio_tokens(text) if factor is not None else []
        stock_ratio = ratios[0] if ratios else None

        low_t = _WITHOUT_PER_SHARE_UNIT_RE.sub("/cp", text).lower()
        has_stock = factor is not None or bool(
            _SHARE_CHANGING_RE.search(_WITHOUT_PER_SHARE_UNIT_RE.sub("/cp", text))
        )
        cash_pct, cash_vnd = _extract_cash_dividend(text, has_stock=has_stock)

        if factor is not None and (cash_pct is not None or cash_vnd is not None):
            subtype = "mixed_dividend"
        elif factor is not None:
            subtype = "share_change"
        elif cash_pct is not None or cash_vnd is not None:
            subtype = "cash_dividend"
        else:
            subtype = "corporate_notice"
        m_shares = _SHARES_COUNT_RE.search(text)
        shares_delta = _parse_locale_number(m_shares.group(1)) if m_shares else None
        is_rev = bool(_REVERSE_SPLIT_RE.search(text))
        is_dilution = (factor is not None and factor > 1.0 and not is_rev) or (
            shares_delta is not None and any(k in low_t for k in ("phát hành", "cbcnv", "esop"))
        )
        ev_hash = content_fingerprint(f"{sym}|{ev_date or idx}|{text}")[:12]
        ev_id = f"cafef:{sym}:{ev_date or 'nodate'}:{ev_hash}"
        if ev_id in seen_ids:
            ev_id = f"{ev_id}:{idx}"[:64]
        seen_ids.add(ev_id)
        records.append(
            CorporateEventRecord(
                event_id=ev_id,
                symbol=sym,
                exchange=exchange,
                event_type="corporate_action",
                event_subtype=subtype,
                severity="high" if is_dilution else "medium",
                risk_level=1 if is_dilution else 0,
                title=f"{sym}: {text}",
                detail=text,
                event_date=ev_date,
                ex_date=ev_date,
                source_id="cafef_events",
                source_url=f"https://cafef.vn/du-lieu/Ajax/PageNew/LichSuKien.ashx?Symbol={sym}",
                is_share_changing=factor is not None,
                share_factor=factor,
                shares_delta=shares_delta,
                is_dilution=is_dilution,
                cash_dividend_pct=cash_pct,
                cash_per_share_vnd=cash_vnd,
                stock_ratio=stock_ratio,
            )
        )
    return records


_COTUC_CURRENCY_WORDS = frozenset(
    {"VND", "USD", "EUR", "JPY", "SGD", "GBP", "CNY", "AUD", "CAD", "CHF", "THB"}
)


def _extract_cotuc_ticker(slug: str, tokens: Sequence[str], valid_tickers: frozenset[str]) -> str:
    """Resolve ticker from a cotuc.vn card URL slug and leading DOM tokens."""
    slug_head = (slug.rsplit("/", 1)[-1].split("-", 1)[0] or "").upper()
    for tok in tokens[:3]:
        t = tok.strip().upper()
        if (
            len(t) == 6
            and t[:3] == t[3:]
            and re.fullmatch(r"[A-Z0-9]{3}", t[:3])
            and (not valid_tickers or t[:3] in valid_tickers)
        ):
            return t[:3]
        if (
            re.fullmatch(r"[A-Z0-9]{3,5}", t)
            and (t not in _COTUC_CURRENCY_WORDS or t == slug_head)
            and (t in valid_tickers if valid_tickers else t not in _AMBIGUOUS_ACRONYMS)
        ):
            return t
    if re.fullmatch(r"[A-Z0-9]{3,5}", slug_head) and (
        slug_head in valid_tickers if valid_tickers else slug_head not in _AMBIGUOUS_ACRONYMS
    ):
        return slug_head
    return ""


def parse_cotuc_calendar_html(
    html_text: str,
    *,
    base_url: str = COTUC_CALENDAR_URL,
    today: date | None = None,
) -> list[CorporateEventRecord]:
    """Parse upcoming dividend & bonus events from ``cotuc.vn/su-kien`` SSR cards.

    Preserves cash dividend percentages/VND amounts and stock ratio strings in
    distinct fields cleanly (even on mixed cash + stock cards), validates href
    dates safely, and extracts record dates before mismatch comparison.
    """
    parser = _AnchorExtractor(split_inline_spans=True)
    parser.feed(html_text or "")
    parser.close()

    ref_day = today or datetime.now(_VN_TZ).date()
    valid_tickers = get_vn_ticker_symbols()
    meta_catalog = load_vn_tickers()
    origin = f"{urlsplit(base_url).scheme}://{urlsplit(base_url).netloc}"

    records: list[CorporateEventRecord] = []
    seen_ids: set[str] = set()

    for href, tokens, _has_img in parser.anchors:
        m_href = re.search(r"/su-kien/(\d{4}-\d{2}-\d{2})/([^/?#]+)", href)
        if not m_href:
            continue
        ex_date = m_href.group(1)
        try:
            ex_day = date.fromisoformat(ex_date)
        except ValueError:
            continue
        slug = m_href.group(2)
        sym = _extract_cotuc_ticker(slug, tokens, valid_tickers)
        if not sym:
            continue

        joined = " ".join(tokens)
        low = joined.lower()
        cash_pct: float | None = None
        cash_vnd: float | None = None
        stock_ratio: str | None = None
        factor: float | None = None
        record_date: str | None = None
        payment_date: str | None = None

        m_rec = re.search(
            r"(?:đkcc|đăng\s+ký\s+cuối\s+cùng|record\s+date)\s*(?:ngày)?\s*[:\s]*(\d{1,2}/\d{1,2}/20\d{2})",
            joined,
            re.IGNORECASE,
        )
        if m_rec:
            record_date = _parse_text_date(m_rec.group(1))

        m_pay = re.search(
            r"(?:thanh\s+toán|thực\s+hiện)(?:\s*dự\s+kiến)?\s*(?:ngày)?\s*[:\s]*(\d{1,2}/\d{1,2}/20\d{2})",
            joined,
            re.IGNORECASE,
        )
        if m_pay:
            payment_date = _parse_text_date(m_pay.group(1))

        date_mismatch: str | None = None
        for m_d in _DMY_DATE_RE.finditer(joined):
            parsed_d = _parse_text_date(m_d.group(0))
            if parsed_d and parsed_d not in (ex_date, record_date, payment_date):
                date_mismatch = f"href_ex_date={ex_date} vs card_date={parsed_d}"
                break

        low_stock = _WITHOUT_PER_SHARE_UNIT_RE.sub("/cp", low)
        has_stock_kw = any(
            k in low_stock
            for k in (
                "cổ phiếu",
                "thưởng",
                "phát hành",
                "bổ sung",
                "ưu đãi",
                "quyền mua",
                "tách",
                "gộp",
                "split",
                "bonus",
                "rights",
            )
        )
        if has_stock_kw:
            factor = parse_share_factor(joined)
            if factor is not None:
                ratios = _extract_ratio_tokens(joined)
                stock_ratio = ratios[0] if ratios else None

        cash_pct, cash_vnd = _extract_cash_dividend(joined, has_stock=has_stock_kw)

        days_until = (ex_day - ref_day).days
        slug_id = (
            slug
            if len(slug) <= 36
            else f"{slug[:24]}-{hashlib.sha256(slug.encode('utf-8')).hexdigest()[:8]}"
        )
        event_id = f"cotuc:{sym}:{ex_date}:{slug_id}"[:64]
        if event_id in seen_ids:
            continue
        seen_ids.add(event_id)

        rec_meta = meta_catalog.get(sym) or {}
        exchange = str(rec_meta.get("exchange") or "").strip() or None
        try:
            abs_url = _safe_http_url(urljoin(origin, href))
        except ValueError:
            abs_url = None
        if factor is not None and (cash_pct is not None or cash_vnd is not None):
            subtype = "mixed_dividend"
        elif factor is not None or (has_stock_kw and cash_pct is None and cash_vnd is None):
            subtype = "stock_entitlement"
        else:
            subtype = "cash_dividend"
        is_rev = bool(_REVERSE_SPLIT_RE.search(joined))
        is_dilution = factor is not None and factor > 1.0 and not is_rev
        is_non_cash = factor is not None or subtype in ("stock_entitlement", "mixed_dividend")
        records.append(
            CorporateEventRecord(
                event_id=event_id,
                symbol=sym,
                exchange=exchange,
                event_type="dividend_calendar",
                event_subtype=subtype,
                severity="medium",
                risk_level=1 if is_non_cash else 0,
                title=f"{sym} ({ex_date}): {joined[:160]}",
                detail=joined[:400],
                event_date=ex_date,
                ex_date=ex_date,
                record_date=record_date,
                payment_date=payment_date,
                days_until=days_until,
                source_id="cotuc_calendar",
                source_url=abs_url,
                is_share_changing=factor is not None,
                share_factor=factor,
                is_dilution=is_dilution,
                cash_dividend_pct=cash_pct,
                cash_per_share_vnd=cash_vnd,
                stock_ratio=stock_ratio,
                date_mismatch_warning=date_mismatch,
            )
        )
    return records


# --------------------------------------------------------------------------------------
# Async circuit-breaker-gated fetch helpers & orchestrators
# --------------------------------------------------------------------------------------

_FEED_HEADERS: dict[str, str] = {
    **BROWSER_HEADERS,
    "Accept": (
        "application/rss+xml, application/xml, text/html, application/xhtml+xml;q=0.9, */*;q=0.8"
    ),
}


def _ordered_ips(ips: frozenset[str] | set[str]) -> list[str]:
    return sorted(ips, key=lambda ip: (1 if ":" in ip else 0, ip))


def _record_parse_outcome(
    breakers: EngineBreakerRegistry,
    breaker_id: str,
    *,
    ok: bool,
) -> None:
    breakers.get(f"events:{breaker_id}").record(Outcome.OK if ok else Outcome.UPSTREAM_ERROR)


async def _fetch_source_text(
    source_id: str,
    url: str,
    *,
    timeout_s: float,
    breakers: EngineBreakerRegistry,
    client: httpx.AsyncClient | None = None,
    breaker_name: str | None = None,
    accept: str | None = None,
    defer_ok: bool = False,
) -> tuple[str | None, str | None]:
    """Fetch bounded UTF-8 text via streaming, IP pinning, identity encoding & SSRF hops."""
    breaker_key = f"events:{breaker_name or source_id}"
    breaker = breakers.get(breaker_key)
    if not breaker.allow():
        return None, f"{source_id}: circuit_open"

    http_client = client or get_http_client("scrape")
    hdrs = dict(_FEED_HEADERS if accept is None else {**BROWSER_HEADERS, "Accept": accept})
    hdrs["Accept-Encoding"] = "identity"
    hdrs["Connection"] = "close"
    cur_url = url
    max_hops = 3
    per_req_timeout = max(0.1, float(timeout_s))

    try:
        async with asyncio.timeout(per_req_timeout):
            for _hop in range(max_hops + 1):
                target_url = cur_url
                req_hdrs = dict(hdrs)
                stream_kwargs: dict[str, Any] = {}
                if client is None:
                    try:
                        resolved_ips = await ssrf.resolve_and_check(cur_url)
                    except (ssrf.SSRFViolation, ValueError):
                        breaker.record(Outcome.UPSTREAM_ERROR)
                        err_tag = "ssrf_redirect_blocked" if _hop > 0 else "ssrf_blocked"
                        return None, f"{source_id}: {err_tag}"
                    ordered = _ordered_ips(resolved_ips)
                    if ordered:
                        target_url, pin_hdrs, pin_ext = pinning.build_pinned_request(
                            cur_url, ordered[0]
                        )
                        req_hdrs.update(pin_hdrs)
                        if pin_ext:
                            stream_kwargs["extensions"] = pin_ext

                async with http_client.stream(
                    "GET",
                    target_url,
                    headers=req_hdrs,
                    follow_redirects=False,
                    timeout=per_req_timeout,
                    **stream_kwargs,
                ) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location")
                        if not loc:
                            breaker.record(Outcome.UPSTREAM_ERROR)
                            return None, f"{source_id}: empty_redirect"
                        if _hop >= max_hops:
                            breaker.record(Outcome.UPSTREAM_ERROR)
                            return None, f"{source_id}: too_many_redirects"
                        try:
                            next_url = urljoin(cur_url, loc.strip())
                            safe_next = _safe_http_url(next_url)
                            if not safe_next:
                                raise ssrf.SSRFViolation("unsafe_redirect_url")
                            await ssrf.resolve_and_check(safe_next)
                        except (ssrf.SSRFViolation, ValueError):
                            breaker.record(Outcome.UPSTREAM_ERROR)
                            return None, f"{source_id}: ssrf_redirect_blocked"
                        cur_url = safe_next
                        continue

                    if resp.status_code == 429:
                        breaker.record(Outcome.RATE_LIMITED)
                        return None, f"{source_id}: rate_limited"
                    if resp.status_code >= 500 or resp.status_code != 200:
                        breaker.record(Outcome.UPSTREAM_ERROR)
                        return None, f"{source_id}: http_{resp.status_code}"

                    ce = (resp.headers.get("content-encoding") or "").strip().lower()
                    if ce and ce not in ("identity", "gzip", "x-gzip", "deflate"):
                        breaker.record(Outcome.UPSTREAM_ERROR)
                        return None, f"{source_id}: unsupported_content_encoding"

                    buf = bytearray()
                    is_pre_consumed = getattr(resp, "is_stream_consumed", False)
                    has_aiter_raw = hasattr(resp, "aiter_raw") and not is_pre_consumed
                    if ce in ("gzip", "x-gzip", "deflate") and has_aiter_raw:
                        wbits = 16 + zlib.MAX_WBITS if "gzip" in ce else zlib.MAX_WBITS
                        decomp = zlib.decompressobj(wbits)
                        raw_len = 0
                        try:
                            async for chunk in resp.aiter_raw():
                                raw_len += len(chunk)
                                if raw_len > MAX_RESPONSE_BYTES:
                                    breaker.record(Outcome.UPSTREAM_ERROR)
                                    return None, f"{source_id}: payload_too_large"
                                rem_bytes = MAX_RESPONSE_BYTES - len(buf)
                                buf.extend(decomp.decompress(chunk, rem_bytes + 1))
                                if len(buf) > MAX_RESPONSE_BYTES or decomp.unconsumed_tail:
                                    breaker.record(Outcome.UPSTREAM_ERROR)
                                    return None, f"{source_id}: payload_too_large"
                            rem_bytes = MAX_RESPONSE_BYTES - len(buf)
                            buf.extend(decomp.flush(rem_bytes + 1))
                            if len(buf) > MAX_RESPONSE_BYTES:
                                breaker.record(Outcome.UPSTREAM_ERROR)
                                return None, f"{source_id}: payload_too_large"
                        except zlib.error:
                            breaker.record(Outcome.UPSTREAM_ERROR)
                            return None, f"{source_id}: bad_compressed_payload"
                    elif has_aiter_raw and ce in ("", "identity"):
                        async for chunk in resp.aiter_raw():
                            buf.extend(chunk)
                            if len(buf) > MAX_RESPONSE_BYTES:
                                breaker.record(Outcome.UPSTREAM_ERROR)
                                return None, f"{source_id}: payload_too_large"
                    else:
                        async for chunk in resp.aiter_bytes():
                            buf.extend(chunk)
                            if len(buf) > MAX_RESPONSE_BYTES:
                                breaker.record(Outcome.UPSTREAM_ERROR)
                                return None, f"{source_id}: payload_too_large"

                    enc = resp.encoding or resp.charset_encoding or "utf-8"
                    if enc.lower() == "iso-8859-1":
                        enc = resp.charset_encoding or "utf-8"
                    raw_bytes = bytes(buf)
                    try:
                        text = raw_bytes.decode(enc, errors="replace")
                    except LookupError:
                        text = raw_bytes.decode("utf-8", errors="replace")
                    if not defer_ok:
                        breaker.record(Outcome.OK)
                    return text, None
    except asyncio.CancelledError:
        breaker.record(Outcome.CANCELLED)
        raise
    except (TimeoutError, httpx.TimeoutException):
        breaker.record(Outcome.TIMEOUT)
        return None, f"{source_id}: timeout"
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        breaker.record(Outcome.UPSTREAM_ERROR)
        return None, f"{source_id}: {type(exc).__name__}"

    breaker.record(Outcome.UPSTREAM_ERROR)
    return None, f"{source_id}: too_many_redirects"


def _build_raw_article_items(
    raw_articles: Sequence[Mapping[str, Any]],
    sym_set: set[str],
) -> list[ParsedNewsItem]:
    items: list[ParsedNewsItem] = []
    valid_scopes = {"ticker", "market", "policy", "sector", "general"}
    valid_cats = {
        "corporate_action",
        "listing_status",
        "insider_trade",
        "regulatory",
        "macro_policy",
        "market_flow",
        "general_finance",
    }
    for idx, doc in enumerate(raw_articles):
        title = str(doc.get("title") or "").strip()
        if not title:
            continue
        url = _safe_http_url(doc.get("url"))
        src_id = str(doc.get("source_id") or doc.get("domain") or "caller").strip()
        snippet = str(doc.get("snippet") or doc.get("content") or "")[:500]
        pub_dt = _parse_rss_datetime(doc.get("published_at"))
        if pub_dt is None and doc.get("published_at"):
            iso_d = _parse_text_date(doc.get("published_at"))
            if iso_d:
                pub_dt = datetime.fromisoformat(iso_d).replace(tzinfo=UTC)
        ev_date = _parse_text_date(doc.get("event_date"))
        explicit_syms = [
            str(s).strip().upper() for s in (doc.get("symbols") or []) if str(s).strip()
        ]
        parsed_syms = explicit_syms or extract_tickers_from_text(
            f"{title} {snippet}", allowed_symbols=sym_set or None
        )
        scope, category, severity = classify_news_scope_and_category(
            title, snippet, symbols=parsed_syms
        )
        if doc.get("scope") in valid_scopes:
            scope = doc["scope"]
        if doc.get("category") in valid_cats:
            category = doc["category"]
        items.append(
            ParsedNewsItem(
                title=title,
                url=url,
                source_id=src_id,
                snippet=snippet,
                published_at=pub_dt,
                event_date=ev_date,
                symbols=parsed_syms,
                scope=scope,
                category=category,
                severity=severity,
                stable_id=stable_document_id(url, source_id=src_id, fallback_text=f"{idx}:{title}"),
                fingerprint=content_fingerprint(title),
            )
        )
    return items


def _remaining_timeout(deadline: float, cap_s: float) -> float:
    budget = get_current_budget()
    rem = deadline - time.monotonic()
    if budget is not None:
        rem = min(rem, budget.remaining())
    return max(0.05, min(cap_s, rem))


def _tag_google_news_hits(
    raw_hits: Sequence[Any],
    tgt: str,
    sym_set: set[str],
    global_syms: set[str],
) -> list[ParsedNewsItem]:
    out_items: list[ParsedNewsItem] = []
    boundary_pat = _symbol_boundary_regex(tgt) if tgt != "__broad__" else None
    for h in raw_hits:
        syms = extract_tickers_from_text(
            f"{h.title} {h.snippet}",
            allowed_symbols=({tgt} | sym_set) if tgt != "__broad__" else (sym_set or None),
        )
        if (
            tgt in global_syms
            and tgt not in syms
            and boundary_pat is not None
            and boundary_pat.search(h.title if len(tgt) <= 2 else f"{h.title} {h.snippet}")
        ):
            syms.insert(0, tgt)
        scope, category, severity = classify_news_scope_and_category(
            h.title, h.snippet, symbols=syms
        )
        out_items.append(
            ParsedNewsItem(
                title=h.title,
                url=_safe_http_url(h.url),
                source_id="google_news",
                snippet=h.snippet[:500],
                published_at=h.published_at,
                symbols=syms,
                scope=scope,
                category=category,
                severity=severity,
                stable_id=stable_document_id(h.url, source_id="google_news", fallback_text=h.title),
                fingerprint=content_fingerprint(h.title),
            )
        )
    return out_items


async def fetch_market_news_wire(
    *,
    symbols: Sequence[str] | None = None,
    lang: str = "auto",
    categories: Sequence[NewsCategory] | None = None,
    include_market_macro: bool = True,
    deduplicate: bool = True,
    max_age_days: int = 7,
    max_items: int = 30,
    seen_fingerprints: Sequence[str] | None = None,
    raw_articles: Sequence[Mapping[str, Any]] | None = None,
    include_search_fallback: bool = False,
    timeout_s: float = 8.0,
    breakers: EngineBreakerRegistry | None = None,
    cache: CacheClient | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Aggregate curated financial news wire with entity tagging, dedup & delta tracking."""
    reg = breakers or EngineBreakerRegistry(threshold=3, timeout_threshold=5)
    budget = get_current_budget()
    effective_timeout = max(0.1, float(timeout_s))
    if budget is not None:
        effective_timeout = max(0.1, min(effective_timeout, max(0.1, budget.remaining())))
    deadline = time.monotonic() + effective_timeout

    normalized_syms: list[str] = []
    vn_syms: list[str] = []
    global_syms: list[str] = []
    vn_catalog = load_vn_tickers()
    for raw_sym in symbols or ():
        s_vn = ensure_symbol(raw_sym)
        s_gl = ensure_global_symbol(raw_sym)
        sym = s_vn or s_gl
        if sym and sym not in normalized_syms:
            normalized_syms.append(sym)
            if is_vn_ticker(sym) or sym in vn_catalog:
                vn_syms.append(sym)
            else:
                global_syms.append(sym)
    sym_set = set(normalized_syms)
    global_sym_set = set(global_syms)
    cat_set = set(categories) if categories is not None else None

    all_items: list[ParsedNewsItem] = []
    sources_used: list[str] = []
    sources_failed: list[str] = []
    warnings: list[str] = []

    if raw_articles is not None:
        sources_used.append("caller_supplied")
        all_items = await run_cpu(_build_raw_article_items, raw_articles, sym_set)
    else:
        run_vn_wire = (lang != "en") and (bool(vn_syms) or not normalized_syms)
        cache_key = f"news_wire:v2:{lang}:{'vn' if run_vn_wire else 'global'}"
        cached_bundle: dict[str, Any] | None = None
        hydrated_cache_items: list[ParsedNewsItem] = []
        hydrated_cache_sources: list[str] = []
        use_cached_bundle = False
        if cache is not None and not include_search_fallback and not global_syms:
            try:
                raw_cached = await cache.get_json(cache_key)
                if isinstance(raw_cached, dict) and isinstance(raw_cached.get("items"), list):
                    cached_bundle = raw_cached
                    raw_used = cached_bundle.get("sources_used")
                    hydrated_cache_sources = (
                        [str(s) for s in raw_used if isinstance(s, str) and s.strip()]
                        if isinstance(raw_used, list)
                        else ["cache"]
                    ) or ["cache"]
                    scope_map: dict[str, EventScope] = {
                        "ticker": "ticker",
                        "market": "market",
                        "policy": "policy",
                        "sector": "sector",
                        "general": "general",
                    }
                    cat_map: dict[str, NewsCategory] = {
                        "corporate_action": "corporate_action",
                        "listing_status": "listing_status",
                        "insider_trade": "insider_trade",
                        "regulatory": "regulatory",
                        "macro_policy": "macro_policy",
                        "market_flow": "market_flow",
                        "general_finance": "general_finance",
                    }
                    sev_map: dict[str, SeverityLevel] = {
                        "critical": "critical",
                        "high": "high",
                        "medium": "medium",
                        "info": "info",
                    }
                    for raw_it in cached_bundle["items"][:500]:
                        if not isinstance(raw_it, dict):
                            continue
                        raw_title = raw_it.get("title")
                        if not isinstance(raw_title, str) or not raw_title.strip():
                            continue
                        raw_syms = raw_it.get("symbols")
                        parsed_syms = (
                            [
                                str(x).strip().upper()
                                for x in raw_syms[:30]
                                if isinstance(x, str) and x.strip()
                            ]
                            if isinstance(raw_syms, list)
                            else []
                        )
                        scope_v = raw_it.get("scope")
                        cat_v = raw_it.get("category")
                        sev_v = raw_it.get("severity")
                        hydrated_cache_items.append(
                            ParsedNewsItem(
                                title=raw_title.strip()[:600],
                                url=_safe_http_url(raw_it.get("url")),
                                source_id=_opt_str(raw_it.get("source_id"), 64) or "",
                                snippet=_opt_str(raw_it.get("snippet"), 1000) or "",
                                published_at=_parse_rss_datetime(
                                    _opt_str(raw_it.get("published_at"), 64)
                                ),
                                event_date=_parse_text_date(raw_it.get("event_date")),
                                symbols=parsed_syms,
                                scope=scope_map.get(scope_v, "general")
                                if isinstance(scope_v, str)
                                else "general",
                                category=cat_map.get(cat_v, "general_finance")
                                if isinstance(cat_v, str)
                                else "general_finance",
                                severity=sev_map.get(sev_v, "info")
                                if isinstance(sev_v, str)
                                else "info",
                                stable_id=_opt_str(raw_it.get("stable_id"), 64) or "",
                                fingerprint=_opt_str(raw_it.get("fingerprint"), 64) or "",
                            )
                        )
                    use_cached_bundle = True
            except Exception as exc:
                logger.warning("news_wire cache read/hydrate failed key=%s err=%s", cache_key, exc)
                hydrated_cache_items = []
                hydrated_cache_sources = []
                use_cached_bundle = False

        if use_cached_bundle:
            sources_used = hydrated_cache_sources
            all_items.extend(hydrated_cache_items)
        else:
            per_call_timeout = _remaining_timeout(deadline, 6.0)
            tasks: list[tuple[str, str, Any]] = []
            if run_vn_wire:
                for sid, url, cat_h, sc_h in RSS_WIRE_SOURCES:
                    tasks.append(("rss", sid, (url, cat_h, sc_h)))
                for sid, url, base_u, cat_h, sc_h in HTML_WIRE_SOURCES:
                    tasks.append(("html", sid, (url, base_u, cat_h, sc_h)))

            async def _run_one(
                kind: str, sid: str, meta: tuple[Any, ...]
            ) -> tuple[str, list[ParsedNewsItem], str | None, str | None]:
                text, err = await _fetch_source_text(
                    sid,
                    meta[0],
                    timeout_s=_remaining_timeout(deadline, per_call_timeout),
                    breakers=reg,
                    client=client,
                    defer_ok=True,
                )
                if err or text is None:
                    return sid, [], err, None
                try:
                    if kind == "rss":
                        parsed = await run_cpu(
                            parse_rss_feed_xml,
                            text,
                            source_id=sid,
                            category_hint=meta[1],
                            scope_hint=meta[2],
                            raise_on_error=True,
                        )
                    else:
                        parsed = await run_cpu(
                            parse_html_article_links,
                            text,
                            source_id=sid,
                            base_url=meta[1],
                            category_hint=meta[2],
                            scope_hint=meta[3],
                        )
                except (XMLParseError, DefusedXmlException):
                    _record_parse_outcome(reg, sid, ok=False)
                    return sid, [], f"{sid}: xml_parse_error", None
                except Exception as exc:
                    _record_parse_outcome(reg, sid, ok=False)
                    return sid, [], f"{sid}: {type(exc).__name__}", None
                warn: str | None = None
                parse_ok = True
                if text.strip() and not parsed:
                    parse_ok = False
                    warn = (
                        f"{sid}: possible_layout_changed "
                        f"(fetched {len(text)} chars, 0 items extracted)"
                    )
                _record_parse_outcome(reg, sid, ok=parse_ok)
                return sid, parsed, None, warn

            async def _run_gn(
                tgt: str, query: str, q_lang: str
            ) -> tuple[str, list[ParsedNewsItem], str | None]:
                gn = GoogleNewsAdapter(client=client)
                raw_hits = await gn.search(
                    query,
                    lang=q_lang,
                    freshness_days=min(max_age_days, 30),
                    max_results=8,
                    timeout_s=_remaining_timeout(deadline, 4.0),
                )
                out_items = await run_cpu(
                    _tag_google_news_hits,
                    raw_hits,
                    tgt,
                    sym_set,
                    global_sym_set,
                )
                return "google_news", out_items, None

            gn_args: list[tuple[str, str, str]] = []
            if (include_search_fallback or global_syms) and _remaining_timeout(
                deadline, 1.0
            ) > 0.25:
                search_targets = global_syms[:4] if global_syms else normalized_syms[:3]
                if len(global_syms) > 4 or (not global_syms and len(normalized_syms) > 3):
                    warnings.append(
                        f"search_fallback_capped: queried {len(search_targets)} of "
                        f"{len(global_syms) or len(normalized_syms)} symbols"
                    )
                if not search_targets:
                    if lang == "en":
                        gn_args.append(("__broad__", '"stock market" finance earnings', "en"))
                    elif lang == "vi":
                        gn_args.append(("__broad__", '"chứng khoán" cổ phiếu thị trường', "vi"))
                    else:
                        gn_args.append(("__broad__", '"stock market" finance earnings', "en"))
                        gn_args.append(("__broad__", '"chứng khoán" cổ phiếu thị trường', "vi"))
                else:
                    for tgt in search_targets:
                        is_gl = tgt in global_syms or lang == "en"
                        q_str = (
                            f'"{tgt}" stock earnings news'
                            if is_gl
                            else f'"{tgt}" cổ phiếu tin tức chứng khoán'
                        )
                        gn_args.append((tgt, q_str, "en" if is_gl else "vi"))

            async def _exec_wire(kind: str, sid: str, meta: tuple[Any, ...]) -> tuple[str, Any]:
                return "wire", await _run_one(kind, sid, meta)

            async def _exec_gn(tgt: str, q_str: str, q_lang: str) -> tuple[str, Any]:
                return "gn", await _run_gn(tgt, q_str, q_lang)

            pending_labels: dict[asyncio.Task[tuple[str, Any]], str] = {}
            wire_res: list[Any] = []
            gn_res: list[Any] = []
            try:
                for kind, sid, meta in tasks:
                    pending_labels[asyncio.create_task(_exec_wire(kind, sid, meta))] = sid
                for tgt, q_str, q_lang in gn_args:
                    pending_labels[asyncio.create_task(_exec_gn(tgt, q_str, q_lang))] = (
                        f"google_news:{tgt}"
                    )

                if pending_labels:
                    done, pending = await asyncio.wait(
                        pending_labels.keys(),
                        timeout=max(0.15, deadline - time.monotonic()),
                    )
                    for t in pending:
                        t.cancel()
                        sources_failed.append(f"{pending_labels[t]}: timeout")
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    for t in done:
                        label = pending_labels[t]
                        try:
                            bucket, payload = t.result()
                            if bucket == "wire":
                                wire_res.append(payload)
                            else:
                                gn_res.append(payload)
                        except BaseException as exc:
                            sources_failed.append(f"{label}:{type(exc).__name__}")
            except BaseException:
                for t in pending_labels:
                    if not t.done():
                        t.cancel()
                if pending_labels:
                    await asyncio.gather(*pending_labels.keys(), return_exceptions=True)
                raise

            wire_only_items: list[ParsedNewsItem] = []
            for res in wire_res:
                sid, parsed_items, err, warn = res
                if err:
                    sources_failed.append(err)
                else:
                    sources_used.append(sid)
                    all_items.extend(parsed_items)
                    wire_only_items.extend(parsed_items)
                    if warn:
                        warnings.append(warn)

            for gres in gn_res:
                sid, parsed_items, err = gres
                if err:
                    sources_failed.append(err)
                else:
                    if sid not in sources_used and parsed_items:
                        sources_used.append(sid)
                    all_items.extend(parsed_items)

            # Never poison full-TTL cache with degraded partial failures.
            if (
                cache is not None
                and wire_only_items
                and not sources_failed
                and not include_search_fallback
                and not global_syms
            ):
                serial = [
                    {
                        "title": it.title,
                        "url": it.url,
                        "source_id": it.source_id,
                        "snippet": it.snippet,
                        "published_at": it.published_at.isoformat() if it.published_at else None,
                        "event_date": it.event_date,
                        "symbols": it.symbols,
                        "scope": it.scope,
                        "category": it.category,
                        "severity": it.severity,
                        "stable_id": it.stable_id,
                        "fingerprint": it.fingerprint,
                    }
                    for it in wire_only_items[:500]
                ]
                with contextlib.suppress(Exception):
                    await cache.set_json(
                        cache_key,
                        {
                            "sources_used": [s for s in sources_used if s != "google_news"],
                            "items": serial,
                        },
                        ttl_s=_NEWS_WIRE_CACHE_TTL_S,
                    )

    total_raw = len(all_items)
    working = await run_cpu(deduplicate_news_items, all_items) if deduplicate else list(all_items)
    filtered = await run_cpu(filter_news_freshness, working, max_age_days=max_age_days)
    if cat_set is not None:
        filtered = [it for it in filtered if it.category in cat_set]
    if sym_set:
        filtered = [
            it
            for it in filtered
            if (sym_set & set(it.symbols))
            or (include_market_macro and it.scope in ("market", "policy"))
        ]
    elif not include_market_macro:
        filtered = [it for it in filtered if it.scope not in ("market", "policy")]

    # Actionable symbol matches first when symbols are requested, then severity & recency.
    _sev_order = {"critical": 0, "high": 1, "medium": 2, "info": 3}
    filtered.sort(
        key=lambda it: (
            (0 if (sym_set & set(it.symbols)) else 1) if sym_set else 0,
            _sev_order.get(it.severity, 3),
            -_safe_dt_timestamp(it.published_at),
        )
    )
    deduplicated_count = len(filtered)
    filtered = filtered[:max_items]

    seen_set = {str(x).strip() for x in (seen_fingerprints or ()) if str(x).strip()}
    new_fps: list[str] = []
    for it in filtered:
        it.is_new = it.fingerprint not in seen_set
        if it.is_new:
            new_fps.append(it.fingerprint)

    return {
        "items": filtered,
        "new_count": len(new_fps),
        "total_raw": total_raw,
        "deduplicated_count": deduplicated_count,
        "sources_used": sources_used,
        "sources_failed": sources_failed,
        "warnings": warnings,
        "partial": bool(sources_failed),
        "new_fingerprints": new_fps,
    }


def _classify_rss_event_record(item: ParsedNewsItem) -> CorporateEventRecord | None:
    """Promote an RSS corporate/listing/insider/regulatory item into ``CorporateEventRecord``."""
    sym = item.symbols[0] if item.symbols else None
    exch = None
    if sym:
        exch = str((load_vn_tickers().get(sym) or {}).get("exchange") or "").strip() or None
    ev_date = item.event_date or (
        _to_vn_date(item.published_at).isoformat() if item.published_at else None
    )

    if item.category == "listing_status":
        lc = classify_listing_event(item.title, item.snippet)
        return CorporateEventRecord(
            event_id=item.stable_id or f"listing:{item.fingerprint}",
            symbol=sym,
            exchange=exch,
            event_type="listing",
            event_subtype=lc.subtype,
            severity=lc.severity,
            risk_level=3 if lc.subtype == "delisting" else (2 if lc.is_risk_alert else 1),
            title=item.title,
            detail=item.snippet,
            event_date=ev_date,
            source_id=item.source_id,
            source_url=item.url,
            shares_delta=lc.shares_delta,
            is_dilution=lc.is_dilution,
        )
    if item.category == "regulatory":
        rc = classify_regulatory_action(item.title, item.snippet)
        return CorporateEventRecord(
            event_id=item.stable_id or f"reg:{item.fingerprint}",
            symbol=sym,
            exchange=exch,
            event_type="regulatory",
            event_subtype=rc.subtype,
            severity=rc.severity,
            risk_level=rc.risk_level,
            title=item.title,
            detail=item.snippet,
            event_date=ev_date,
            source_id=item.source_id,
            source_url=item.url,
        )
    if item.category == "insider_trade":
        ic = classify_insider_trade(item.title, item.snippet)
        return CorporateEventRecord(
            event_id=item.stable_id or f"insider:{item.fingerprint}",
            symbol=sym,
            exchange=exch,
            event_type="insider",
            event_subtype=f"{ic.direction}_{ic.execution_status}",
            severity="medium",
            risk_level=1 if (ic.direction == "sell" and ic.execution_status != "cancelled") else 0,
            title=item.title,
            detail=item.snippet,
            event_date=ev_date,
            source_id=item.source_id,
            source_url=item.url,
            shares_delta=ic.shares_count,
        )
    if item.category in ("macro_policy", "market_flow"):
        return None

    text_comb = f"{item.title} {item.snippet}"
    factor = parse_share_factor(text_comb)
    if item.category != "corporate_action" and factor is None:
        return None
    is_rev = bool(_REVERSE_SPLIT_RE.search(text_comb))
    return CorporateEventRecord(
        event_id=item.stable_id or f"corp:{item.fingerprint}",
        symbol=sym,
        exchange=exch,
        event_type="corporate_action",
        event_subtype="dividend_or_capital" if factor else "corporate_notice",
        severity="high" if factor else item.severity,
        risk_level=1 if factor else 0,
        title=item.title,
        detail=item.snippet,
        event_date=ev_date,
        source_id=item.source_id,
        source_url=item.url,
        is_share_changing=factor is not None,
        share_factor=factor,
        is_dilution=factor is not None and factor > 1.0 and not is_rev,
    )


def _classify_rss_events_batch(parsed_news: Sequence[ParsedNewsItem]) -> list[CorporateEventRecord]:
    return [ev for n in parsed_news if (ev := _classify_rss_event_record(n)) is not None]


def _opt_str(v: Any, max_len: int = 1000) -> str | None:
    if not isinstance(v, str):
        return None
    s = v.strip()
    return s[:max_len] if s else None


def _opt_finite_float(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


_EV_TYPE_MAP: dict[
    str, Literal["corporate_action", "dividend_calendar", "listing", "insider", "regulatory"]
] = {
    "corporate_action": "corporate_action",
    "dividend_calendar": "dividend_calendar",
    "listing": "listing",
    "insider": "insider",
    "regulatory": "regulatory",
}
_SEV_MAP: dict[str, SeverityLevel] = {
    "critical": "critical",
    "high": "high",
    "medium": "medium",
    "info": "info",
}


def _deserialize_event_records(
    raw_list: Sequence[Any],
    *,
    ref_day: date | None = None,
) -> list[CorporateEventRecord]:
    if not isinstance(raw_list, Sequence) or isinstance(raw_list, (str, bytes)):
        return []
    day_now = ref_day or datetime.now(_VN_TZ).date()
    out: list[CorporateEventRecord] = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        ev_id = _opt_str(raw.get("event_id"), 128)
        title = _opt_str(raw.get("title"), 600)
        ev_type = raw.get("event_type")
        ev_subtype = _opt_str(raw.get("event_subtype"), 64)
        severity = raw.get("severity")
        risk_raw = raw.get("risk_level")
        typed_ev = _EV_TYPE_MAP.get(ev_type) if isinstance(ev_type, str) else None
        typed_sev = _SEV_MAP.get(severity) if isinstance(severity, str) else None
        if (
            not ev_id
            or not title
            or typed_ev is None
            or not ev_subtype
            or typed_sev is None
            or isinstance(risk_raw, bool)
            or not isinstance(risk_raw, int)
        ):
            continue
        ex_date = _parse_text_date(raw.get("ex_date"))
        ev_date = _parse_text_date(raw.get("event_date"))
        rec_date = _parse_text_date(raw.get("record_date"))
        pay_date = _parse_text_date(raw.get("payment_date"))
        eff_day = ex_date or ev_date
        days_until: int | None = None
        if eff_day:
            with contextlib.suppress(ValueError):
                days_until = (date.fromisoformat(eff_day) - day_now).days
        is_share_changing = raw.get("is_share_changing")
        share_chg_flag = is_share_changing if isinstance(is_share_changing, bool) else False
        is_dilution = raw.get("is_dilution")
        dil_flag = is_dilution if isinstance(is_dilution, bool) else False
        out.append(
            CorporateEventRecord(
                event_id=ev_id,
                symbol=_opt_str(raw.get("symbol"), 16),
                exchange=_opt_str(raw.get("exchange"), 16),
                event_type=typed_ev,
                event_subtype=ev_subtype,
                severity=typed_sev,
                risk_level=max(0, min(10, risk_raw)),
                title=title,
                detail=_opt_str(raw.get("detail"), 4000) or "",
                event_date=ev_date,
                ex_date=ex_date,
                record_date=rec_date,
                payment_date=pay_date,
                days_until=days_until,
                source_id=_opt_str(raw.get("source_id"), 64) or "",
                source_url=_safe_http_url(raw.get("source_url")),
                is_share_changing=share_chg_flag,
                share_factor=_opt_finite_float(raw.get("share_factor")),
                shares_delta=_opt_finite_float(raw.get("shares_delta")),
                is_dilution=dil_flag,
                cash_dividend_pct=_opt_finite_float(raw.get("cash_dividend_pct")),
                cash_per_share_vnd=_opt_finite_float(raw.get("cash_per_share_vnd")),
                stock_ratio=_opt_str(raw.get("stock_ratio"), 64),
                date_mismatch_warning=_opt_str(raw.get("date_mismatch_warning"), 256),
            )
        )
    return out


def _serialize_event_records(evs: Sequence[CorporateEventRecord]) -> list[dict[str, Any]]:
    return [
        {
            "event_id": e.event_id,
            "symbol": e.symbol,
            "exchange": e.exchange,
            "event_type": e.event_type,
            "event_subtype": e.event_subtype,
            "severity": e.severity,
            "risk_level": e.risk_level,
            "title": e.title,
            "detail": e.detail,
            "event_date": e.event_date,
            "ex_date": e.ex_date,
            "record_date": e.record_date,
            "payment_date": e.payment_date,
            "days_until": e.days_until,
            "source_id": e.source_id,
            "source_url": e.source_url,
            "is_share_changing": e.is_share_changing,
            "share_factor": e.share_factor,
            "shares_delta": e.shares_delta,
            "is_dilution": e.is_dilution,
            "cash_dividend_pct": e.cash_dividend_pct,
            "cash_per_share_vnd": e.cash_per_share_vnd,
            "stock_ratio": e.stock_ratio,
            "date_mismatch_warning": e.date_mismatch_warning,
        }
        for e in evs[:_MAX_FEED_ITEMS]
    ]


def _build_raw_notice_events(
    raw_notices: Sequence[Mapping[str, Any]],
    sym_set: set[str],
) -> list[CorporateEventRecord]:
    events: list[CorporateEventRecord] = []
    vn_catalog = load_vn_tickers()
    for idx, notice in enumerate(raw_notices):
        title = str(notice.get("title") or "").strip()
        if not title:
            continue
        detail = str(notice.get("detail") or notice.get("text") or "").strip()
        ev_date = _parse_text_date(notice.get("event_date")) or _parse_text_date(
            f"{title} {detail}"
        )
        raw_notice_sym = str(notice.get("symbol") or "").strip()
        src_sym = (
            (ensure_symbol(raw_notice_sym) or ensure_global_symbol(raw_notice_sym))
            if raw_notice_sym
            else None
        )
        extracted = (
            [src_sym]
            if src_sym
            else extract_tickers_from_text(f"{title} {detail}", allowed_symbols=sym_set or None)
        )
        sym = extracted[0] if extracted else None
        exch = (
            str(
                notice.get("exchange") or (vn_catalog.get(sym or "") or {}).get("exchange") or ""
            ).strip()
            or None
        )
        hint_type = str(notice.get("event_type") or "").strip().lower()
        src_id = str(notice.get("source_id") or "caller_supplied").strip() or "caller_supplied"
        src_url = _safe_http_url(notice.get("source_url") or notice.get("url"))

        reg_probe = classify_regulatory_action(title, detail)
        if hint_type == "regulatory" or (not hint_type and reg_probe.risk_level >= 3):
            events.append(
                CorporateEventRecord(
                    event_id=str(notice.get("event_id") or "")
                    or stable_document_id(
                        src_url,
                        source_id="reg_caller",
                        symbol=sym,
                        fallback_text=f"{idx}:{title}",
                    ),
                    symbol=sym,
                    exchange=exch,
                    event_type="regulatory",
                    event_subtype=reg_probe.subtype,
                    severity=reg_probe.severity,
                    risk_level=reg_probe.risk_level,
                    title=title,
                    detail=detail,
                    event_date=ev_date,
                    source_id=src_id,
                    source_url=src_url,
                )
            )
            continue

        is_listing_notice = hint_type == "listing" or (
            not hint_type
            and any(
                k in f"{title} {detail}".lower()
                for k in (
                    "niêm yết",
                    "hủy niêm yết",
                    "huỷ niêm yết",
                    "chấm dứt niêm yết",
                    "diện cảnh báo",
                    "diện kiểm soát",
                    "ra khỏi diện",
                )
            )
        )
        if is_listing_notice:
            lc = classify_listing_event(title, detail)
            events.append(
                CorporateEventRecord(
                    event_id=str(notice.get("event_id") or "")
                    or stable_document_id(
                        src_url,
                        source_id="list_caller",
                        symbol=sym,
                        fallback_text=f"{idx}:{title}",
                    ),
                    symbol=sym,
                    exchange=exch,
                    event_type="listing",
                    event_subtype=lc.subtype,
                    severity=lc.severity,
                    risk_level=3 if lc.subtype == "delisting" else (2 if lc.is_risk_alert else 1),
                    title=title,
                    detail=detail,
                    event_date=ev_date,
                    source_id=src_id,
                    source_url=src_url,
                    shares_delta=lc.shares_delta,
                    is_dilution=lc.is_dilution,
                )
            )
            continue

        if not hint_type and reg_probe.risk_level >= 1:
            events.append(
                CorporateEventRecord(
                    event_id=str(notice.get("event_id") or "")
                    or stable_document_id(
                        src_url,
                        source_id="reg_caller",
                        symbol=sym,
                        fallback_text=f"{idx}:{title}",
                    ),
                    symbol=sym,
                    exchange=exch,
                    event_type="regulatory",
                    event_subtype=reg_probe.subtype,
                    severity=reg_probe.severity,
                    risk_level=reg_probe.risk_level,
                    title=title,
                    detail=detail,
                    event_date=ev_date,
                    source_id=src_id,
                    source_url=src_url,
                )
            )
            continue

        if hint_type == "insider":
            ic = classify_insider_trade(title, detail)
            events.append(
                CorporateEventRecord(
                    event_id=str(notice.get("event_id") or "")
                    or stable_document_id(
                        src_url,
                        source_id="ins_caller",
                        symbol=sym,
                        fallback_text=f"{idx}:{title}",
                    ),
                    symbol=sym,
                    exchange=exch,
                    event_type="insider",
                    event_subtype=f"{ic.direction}_{ic.execution_status}",
                    severity="medium",
                    risk_level=1
                    if (ic.direction == "sell" and ic.execution_status != "cancelled")
                    else 0,
                    title=title,
                    detail=detail,
                    event_date=ev_date,
                    source_id=src_id,
                    source_url=src_url,
                    shares_delta=ic.shares_count,
                )
            )
            continue

        ex_date = _parse_text_date(notice.get("ex_date"))
        rec_date = _parse_text_date(notice.get("record_date"))
        pay_date = _parse_text_date(notice.get("payment_date"))
        eff_date = ex_date or ev_date
        raw_ratio_str = str(notice.get("stock_ratio") or "").strip()
        text_comb = f"{title} {detail}"
        is_rev = bool(_REVERSE_SPLIT_RE.search(text_comb)) or bool(
            raw_ratio_str and _REVERSE_SPLIT_RE.search(raw_ratio_str)
        )
        is_fwd = (not is_rev) and (
            bool(_FORWARD_SPLIT_RE.search(text_comb))
            or bool(raw_ratio_str and _FORWARD_SPLIT_RE.search(raw_ratio_str))
        )
        factor: float | None = None
        if raw_ratio_str:
            if is_rev:
                probe_str = (
                    raw_ratio_str
                    if _SHARE_CHANGING_RE.search(raw_ratio_str)
                    and _REVERSE_SPLIT_RE.search(raw_ratio_str)
                    else f"gộp cổ phiếu {raw_ratio_str}"
                )
            elif is_fwd:
                probe_str = (
                    raw_ratio_str
                    if _SHARE_CHANGING_RE.search(raw_ratio_str)
                    and _FORWARD_SPLIT_RE.search(raw_ratio_str)
                    else f"tách cổ phiếu {raw_ratio_str}"
                )
            else:
                probe_str = f"thưởng cổ phiếu {raw_ratio_str}"
            factor = parse_share_factor(probe_str)
        if factor is None:
            factor = parse_share_factor(text_comb)
        ratios = (
            (_extract_ratio_tokens(raw_ratio_str) or _extract_ratio_tokens(text_comb))
            if factor is not None
            else []
        )
        is_dil = factor is not None and factor > 1.0 and not is_rev
        ev_type: Literal["corporate_action", "dividend_calendar"] = (
            "dividend_calendar"
            if hint_type == "dividend_calendar" or ex_date
            else "corporate_action"
        )
        text_cash_pct: float | None = None
        text_cash_vnd: float | None = None
        if notice.get("cash_dividend_pct") is None or notice.get("cash_per_share_vnd") is None:
            norm_notice_text = _WITHOUT_PER_SHARE_UNIT_RE.sub("/cp", text_comb)
            has_stock_notice = factor is not None or bool(
                _SHARE_CHANGING_RE.search(norm_notice_text)
            )
            text_cash_pct, text_cash_vnd = _extract_cash_dividend(
                text_comb, has_stock=has_stock_notice
            )
        cash_pct_out = (
            notice.get("cash_dividend_pct")
            if notice.get("cash_dividend_pct") is not None
            else text_cash_pct
        )
        cash_vnd_out = (
            notice.get("cash_per_share_vnd")
            if notice.get("cash_per_share_vnd") is not None
            else text_cash_vnd
        )
        events.append(
            CorporateEventRecord(
                event_id=str(notice.get("event_id") or "")
                or stable_document_id(
                    src_url, source_id="corp_caller", symbol=sym, fallback_text=f"{idx}:{title}"
                ),
                symbol=sym,
                exchange=exch,
                event_type=ev_type,
                event_subtype=(
                    "dividend_or_issue"
                    if ev_type == "dividend_calendar"
                    else ("share_change" if factor else "corporate_notice")
                ),
                severity="high" if factor else "medium",
                risk_level=1 if factor else 0,
                title=title,
                detail=detail,
                event_date=eff_date,
                ex_date=ex_date,
                record_date=rec_date,
                payment_date=pay_date,
                source_id=src_id,
                source_url=src_url,
                is_share_changing=factor is not None,
                share_factor=factor,
                is_dilution=is_dil,
                cash_dividend_pct=cash_pct_out,
                cash_per_share_vnd=cash_vnd_out,
                stock_ratio=raw_ratio_str or (ratios[0] if ratios else None),
            )
        )
    return events


def _classify_gn_reg_hits(
    hits: Sequence[Any],
    sym: str,
    exch: str | None,
    fin_reg_ctx: tuple[str, ...],
) -> list[CorporateEventRecord]:
    out_evs: list[CorporateEventRecord] = []
    sym_boundary = _symbol_boundary_regex(sym)
    for h in hits:
        combined = f"{h.title} {h.snippet}"
        if not sym_boundary.search(combined):
            continue
        low_c = combined.lower()
        if not any(ctx in low_c for ctx in fin_reg_ctx):
            continue
        rc = classify_regulatory_action(h.title, h.snippet)
        if rc.risk_level == 0:
            continue
        ev_d = _to_vn_date(h.published_at).isoformat() if h.published_at else None
        out_evs.append(
            CorporateEventRecord(
                event_id=stable_document_id(
                    h.url, source_id="gn_reg", symbol=sym, fallback_text=h.title
                ),
                symbol=sym,
                exchange=exch,
                event_type="regulatory",
                event_subtype=rc.subtype,
                severity=rc.severity,
                risk_level=rc.risk_level,
                title=h.title,
                detail=h.snippet[:400],
                event_date=ev_d,
                source_id="google_news_regulatory",
                source_url=_safe_http_url(h.url),
            )
        )
    return out_evs


async def fetch_market_events(
    *,
    symbols: Sequence[str] | None = None,
    include_corporate_actions: bool = True,
    include_calendar: bool = True,
    include_listing: bool = True,
    include_insider: bool = True,
    include_regulatory: bool = True,
    lookback_days: int = 365,
    lookahead_days: int = 60,
    reported_share_ratio: float | None = None,
    ratio_tolerance: float = 0.08,
    seen_event_ids: Sequence[str] | None = None,
    raw_notices: Sequence[Mapping[str, Any]] | None = None,
    timeout_s: float = 10.0,
    breakers: EngineBreakerRegistry | None = None,
    cache: CacheClient | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Aggregate corporate actions, ex-dates, listing/regulatory risk & share-factor proofs."""
    reg = breakers or EngineBreakerRegistry(threshold=3, timeout_threshold=5)
    budget = get_current_budget()
    effective_timeout = max(0.1, float(timeout_s))
    if budget is not None:
        effective_timeout = max(0.1, min(effective_timeout, max(0.1, budget.remaining())))
    deadline = time.monotonic() + effective_timeout

    normalized_syms: list[str] = []
    vn_catalog = load_vn_tickers()
    vn_syms: list[str] = []
    for raw_sym in symbols or ():
        s = ensure_symbol(raw_sym) or ensure_global_symbol(raw_sym)
        if s and s not in normalized_syms:
            normalized_syms.append(s)
            if is_vn_ticker(s) or s in vn_catalog:
                vn_syms.append(s)
    sym_set = set(normalized_syms)
    ref_day = datetime.now(_VN_TZ).date()

    all_events: list[CorporateEventRecord] = []
    sources_used: list[str] = []
    sources_failed: list[str] = []
    warnings: list[str] = []

    if raw_notices is not None:
        sources_used.append("caller_supplied")
        all_events = await run_cpu(_build_raw_notice_events, raw_notices, sym_set)
    else:
        per_call_timeout = _remaining_timeout(deadline, 7.0)

        async def _one_cafef(sym: str) -> tuple[str, list[CorporateEventRecord], str | None]:
            if cache is not None:
                try:
                    raw_c = await cache.get_json(f"cafef_events:v2:{sym}")
                    if isinstance(raw_c, dict) and isinstance(raw_c.get("events"), list):
                        return (
                            f"cafef_events:{sym}",
                            _deserialize_event_records(raw_c["events"], ref_day=ref_day),
                            None,
                        )
                except Exception as exc:
                    logger.warning("cafef cache read failed sym=%s err=%s", sym, exc)
            url = f"{CAFEF_EVENTS_URL}?Symbol={sym}&PageIndex=1&PageSize=40"
            txt, err = await _fetch_source_text(
                f"cafef_events:{sym}",
                url,
                timeout_s=_remaining_timeout(deadline, per_call_timeout),
                breakers=reg,
                client=client,
                breaker_name="cafef_events",
                accept="application/json",
                defer_ok=True,
            )
            if err or txt is None:
                return f"cafef_events:{sym}", [], err
            try:
                import json as _json

                parsed_json = _json.loads(txt)
            except ValueError:
                _record_parse_outcome(reg, "cafef_events", ok=False)
                return f"cafef_events:{sym}", [], f"cafef_events:{sym}: bad_json"
            if not isinstance(parsed_json, dict) or not isinstance(parsed_json.get("Data"), list):
                _record_parse_outcome(reg, "cafef_events", ok=False)
                return f"cafef_events:{sym}", [], f"cafef_events:{sym}: bad_payload"
            _record_parse_outcome(reg, "cafef_events", ok=True)
            evs = await run_cpu(parse_cafef_corporate_actions, parsed_json, sym)
            if cache is not None:
                with contextlib.suppress(Exception):
                    await cache.set_json(
                        f"cafef_events:v2:{sym}",
                        {"events": _serialize_event_records(evs)},
                        ttl_s=_EVENTS_CACHE_TTL_S if evs else 300,
                    )
            return f"cafef_events:{sym}", evs, None

        async def _one_cotuc() -> tuple[str, list[CorporateEventRecord], str | None, str | None]:
            try:
                if cache is not None:
                    try:
                        raw_c = await cache.get_json("cotuc_calendar:v2")
                        if isinstance(raw_c, dict) and isinstance(raw_c.get("events"), list):
                            cached_evs = _deserialize_event_records(
                                raw_c["events"], ref_day=ref_day
                            )
                            if sym_set:
                                cached_evs = [e for e in cached_evs if e.symbol in sym_set]
                            return "cotuc_calendar", cached_evs, None, None
                    except Exception as exc:
                        logger.warning("cotuc cache read failed err=%s", exc)
                cotuc_html, cotuc_err = await _fetch_source_text(
                    "cotuc_calendar",
                    COTUC_CALENDAR_URL,
                    timeout_s=_remaining_timeout(deadline, per_call_timeout),
                    breakers=reg,
                    client=client,
                    defer_ok=True,
                )
                if cotuc_err or cotuc_html is None:
                    return (
                        "cotuc_calendar",
                        [],
                        cotuc_err or "cotuc_calendar: unavailable",
                        None,
                    )
                cal_events = await run_cpu(parse_cotuc_calendar_html, cotuc_html, today=ref_day)
                warn_msg: str | None = None
                parse_ok = True
                if len(cotuc_html) > 1000 and not cal_events:
                    parse_ok = False
                    warn_msg = "cotuc_calendar: fetched HTML but extracted 0 calendar cards"
                _record_parse_outcome(reg, "cotuc_calendar", ok=parse_ok)
                if cache is not None and cal_events:
                    with contextlib.suppress(Exception):
                        await cache.set_json(
                            "cotuc_calendar:v2",
                            {"events": _serialize_event_records(cal_events)},
                            ttl_s=_NEWS_WIRE_CACHE_TTL_S,
                        )
                if sym_set:
                    cal_events = [e for e in cal_events if e.symbol in sym_set]
                return "cotuc_calendar", cal_events, None, warn_msg
            except Exception as exc:
                _record_parse_outcome(reg, "cotuc_calendar", ok=False)
                return "cotuc_calendar", [], f"cotuc_calendar:{type(exc).__name__}", None

        async def _one_rss_ev(
            spec: tuple[str, str, NewsCategory, EventScope],
        ) -> tuple[str, list[CorporateEventRecord], str | None]:
            sid, url, cat_h, sc_h = spec
            if cache is not None:
                try:
                    raw_c = await cache.get_json(f"rss_events:v2:{sid}")
                    if isinstance(raw_c, dict) and isinstance(raw_c.get("events"), list):
                        cached_evs = _deserialize_event_records(raw_c["events"], ref_day=ref_day)
                        if sym_set:
                            cached_evs = [e for e in cached_evs if e.symbol in sym_set]
                        return sid, cached_evs, None
                except Exception as exc:
                    logger.warning("rss_events cache read failed sid=%s err=%s", sid, exc)
            txt, err = await _fetch_source_text(
                sid,
                url,
                timeout_s=_remaining_timeout(deadline, per_call_timeout),
                breakers=reg,
                client=client,
                defer_ok=True,
            )
            if err or txt is None:
                return sid, [], err
            try:
                parsed_news = await run_cpu(
                    parse_rss_feed_xml,
                    txt,
                    source_id=sid,
                    category_hint=cat_h,
                    scope_hint=sc_h,
                    raise_on_error=True,
                )
            except (XMLParseError, DefusedXmlException):
                _record_parse_outcome(reg, sid, ok=False)
                return sid, [], f"{sid}: xml_parse_error"
            except Exception as exc:
                _record_parse_outcome(reg, sid, ok=False)
                return sid, [], f"{sid}: {type(exc).__name__}"
            _record_parse_outcome(reg, sid, ok=True)
            evs_all = await run_cpu(_classify_rss_events_batch, parsed_news)
            if cache is not None and evs_all:
                with contextlib.suppress(Exception):
                    await cache.set_json(
                        f"rss_events:v2:{sid}",
                        {"events": _serialize_event_records(evs_all)},
                        ttl_s=_NEWS_WIRE_CACHE_TTL_S,
                    )
            evs = [e for e in evs_all if e.symbol in sym_set] if sym_set else evs_all
            return sid, evs, None

        async def _one_gn_reg(sym: str) -> tuple[str, list[CorporateEventRecord], str | None]:
            is_vn = is_vn_ticker(sym) or sym in vn_catalog
            q_reg = (
                (
                    f'"{sym}" ("UBCKNN" OR "xử phạt" OR "diện cảnh báo" '
                    f'OR "hủy niêm yết" OR site:ssc.gov.vn OR site:hsx.vn OR site:hnx.vn)'
                )
                if is_vn
                else (
                    f'"{sym}" ("SEC enforcement" OR "trading halt" '
                    f'OR delisting OR "deficiency notice" site:sec.gov)'
                )
            )
            _fin_reg_ctx = (
                "chứng khoán",
                "cổ phiếu",
                "ubcknn",
                "ssc",
                "sec",
                "hsx",
                "hose",
                "hnx",
                "upcom",
                "sở giao dịch",
                "niêm yết",
                "công bố thông tin",
                "vi phạm hành chính",
                "hđqt",
                "cổ đông",
                "stock",
                "equity",
                "trading halt",
                "delist",
                "enforcement",
            )
            gn = GoogleNewsAdapter(client=client)
            hits = await gn.search(
                q_reg,
                lang="vi" if is_vn else "en",
                freshness_days=min(lookback_days, 365),
                max_results=5,
                timeout_s=_remaining_timeout(deadline, min(3.5, per_call_timeout)),
            )
            exch = str((vn_catalog.get(sym) or {}).get("exchange") or "") or None
            out_evs = await run_cpu(_classify_gn_reg_hits, hits, sym, exch, _fin_reg_ctx)
            return "google_news_regulatory", out_evs, None

        cafef_syms: list[str] = []
        if include_corporate_actions and vn_syms:
            cafef_syms = vn_syms[:10]
            if len(vn_syms) > 10:
                warnings.append(
                    f"cafef_events_capped: queried {len(cafef_syms)} of {len(vn_syms)} VN symbols"
                )

        rss_targets: list[tuple[str, str, NewsCategory, EventScope]] = []
        if include_listing:
            rss_targets.append(RSS_WIRE_SOURCES[3])  # vietstock_listing
        if include_insider:
            rss_targets.append(RSS_WIRE_SOURCES[2])  # vietstock_insider
        if include_corporate_actions and not normalized_syms:
            rss_targets.append(RSS_WIRE_SOURCES[1])  # vietstock_dividends
        if include_regulatory:
            rss_targets.append(RSS_WIRE_SOURCES[5])  # vietstock_policy

        gn_syms: list[str] = []
        if include_regulatory and normalized_syms and _remaining_timeout(deadline, 1.0) > 0.25:
            gn_syms = normalized_syms[:3]
            if len(normalized_syms) > 3:
                warnings.append(
                    "google_news_regulatory_capped: "
                    f"queried {len(gn_syms)} of {len(normalized_syms)} symbols"
                )

        async def _wrap_ev(bucket: str, coro: Any) -> tuple[str, Any]:
            return bucket, await coro

        pending_map: dict[asyncio.Task[tuple[str, Any]], str] = {}
        cafef_res: list[Any] = []
        cotuc_res: Any = None
        rss_res: list[Any] = []
        gn_res: list[Any] = []
        try:
            for s in cafef_syms:
                pending_map[asyncio.create_task(_wrap_ev("cafef", _one_cafef(s)))] = (
                    f"cafef_events:{s}"
                )
            if include_calendar:
                pending_map[asyncio.create_task(_wrap_ev("cotuc", _one_cotuc()))] = "cotuc_calendar"
            for spec in rss_targets:
                pending_map[asyncio.create_task(_wrap_ev("rss", _one_rss_ev(spec)))] = spec[0]
            for s in gn_syms:
                pending_map[asyncio.create_task(_wrap_ev("gn", _one_gn_reg(s)))] = (
                    f"google_news_regulatory:{s}"
                )

            if pending_map:
                done, pending = await asyncio.wait(
                    pending_map.keys(),
                    timeout=max(0.15, deadline - time.monotonic()),
                )
                for t in pending:
                    t.cancel()
                    sources_failed.append(f"{pending_map[t]}: timeout")
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                for t in done:
                    label = pending_map[t]
                    try:
                        bucket, payload = t.result()
                        if bucket == "cafef":
                            cafef_res.append(payload)
                        elif bucket == "cotuc":
                            cotuc_res = payload
                        elif bucket == "rss":
                            rss_res.append(payload)
                        else:
                            gn_res.append(payload)
                    except BaseException as exc:
                        sources_failed.append(f"{label}:{type(exc).__name__}")
        except BaseException:
            for t in pending_map:
                if not t.done():
                    t.cancel()
            if pending_map:
                await asyncio.gather(*pending_map.keys(), return_exceptions=True)
            raise

        for cr in cafef_res:
            sid, evs, err = cr
            if err:
                sources_failed.append(err)
            else:
                if "cafef_events" not in sources_used:
                    sources_used.append("cafef_events")
                all_events.extend(evs)

        if cotuc_res is not None:
            sid, evs, err, warn_msg = cotuc_res
            if err:
                sources_failed.append(err)
            else:
                sources_used.append(sid)
                all_events.extend(evs)
                if warn_msg:
                    warnings.append(warn_msg)

        for rr in rss_res:
            sid, evs, err = rr
            if err:
                sources_failed.append(err)
            else:
                sources_used.append(sid)
                all_events.extend(evs)

        for gr in gn_res:
            sid, evs, err = gr
            if err:
                sources_failed.append(err)
            else:
                if evs and sid not in sources_used:
                    sources_used.append(sid)
                all_events.extend(evs)

    # Window filter, symbol scope enforcement & dedup
    min_day = (ref_day - timedelta(days=lookback_days)).isoformat()
    max_day = (ref_day + timedelta(days=lookahead_days)).isoformat()

    allowed_event_types: set[str] = set()
    if include_corporate_actions:
        allowed_event_types.add("corporate_action")
    if include_calendar:
        allowed_event_types.add("dividend_calendar")
    if include_listing:
        allowed_event_types.add("listing")
    if include_insider:
        allowed_event_types.add("insider")
    if include_regulatory:
        allowed_event_types.add("regulatory")

    deduped: list[CorporateEventRecord] = []
    seen_ev_keys: set[str] = set()
    for ev in all_events:
        if sym_set and ev.symbol not in sym_set:
            continue
        keep_for_ratio = reported_share_ratio is not None and ev.is_share_changing
        if ev.event_type not in allowed_event_types and not keep_for_ratio:
            continue
        if ev.event_date and not (min_day <= ev.event_date <= max_day) and not keep_for_ratio:
            continue
        if ev.event_id in seen_ev_keys:
            continue
        seen_ev_keys.add(ev.event_id)
        eff_d = ev.ex_date or ev.event_date
        if eff_d:
            with contextlib.suppress(ValueError):
                ev.days_until = (date.fromisoformat(eff_d) - ref_day).days
        deduped.append(ev)

    _sev_order = {"critical": 0, "high": 1, "medium": 2, "info": 3}
    deduped.sort(
        key=lambda e: (
            _sev_order.get(e.severity, 3),
            -_safe_date_ordinal(e.event_date),
        )
    )

    seen_client = {str(x).strip() for x in (seen_event_ids or ()) if str(x).strip()}
    new_ids: list[str] = []
    for ev in deduped:
        ev.is_new = ev.event_id not in seen_client
        if ev.is_new:
            new_ids.append(ev.event_id)

    upcoming_ex = sorted(
        [
            e
            for e in deduped
            if e.ex_date is not None
            and e.days_until is not None
            and 0 <= e.days_until <= lookahead_days
        ],
        key=lambda e: e.days_until if e.days_until is not None else 9999,
    )
    dilution_alerts = [e for e in deduped if e.is_dilution]
    risk_alerts = [e for e in deduped if e.severity in ("critical", "high") or e.risk_level >= 2]

    ratio_proof = (
        explain_share_ratio(deduped, reported_share_ratio, tolerance=ratio_tolerance)
        if reported_share_ratio is not None
        else None
    )

    return {
        "symbols": normalized_syms,
        "events": deduped,
        "upcoming_ex_dates": upcoming_ex,
        "dilution_alerts": dilution_alerts,
        "risk_alerts": risk_alerts,
        "share_ratio_explanation": ratio_proof,
        "new_events_count": len(new_ids),
        "new_event_ids": new_ids,
        "sources_used": sources_used,
        "sources_failed": sources_failed,
        "warnings": warnings,
        "partial": bool(sources_failed),
    }
