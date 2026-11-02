"""Vietnamese listed stock tickers catalog and token recognition (WP-09).

Loads official listed symbols from vn_tickers.json (>= 1,500 active symbols).
Provides fast O(1) membership checking and filters out stop words.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent / "data" / "vn_tickers.json"

# Common stopwords (technical, accounting terms, and common 3-letter English words)
# that should not be falsely recognized as stock tickers.
_VIET_STOCK_STOP_WORDS: frozenset[str] = frozenset(
    {
        # Common English 3-letter words
        "THE",
        "AND",
        "ARE",
        "FOR",
        "NOT",
        "BUT",
        "CAN",
        "ALL",
        "ANY",
        "NEW",
        "NOW",
        "ONE",
        "TWO",
        "OUT",
        "OUR",
        "BIG",
        "LOW",
        "MID",
        "MAX",
        "MIN",
        "FIX",
        "SUB",
        "SET",
        "GET",
        "PUT",
        "RUN",
        "OFF",
        "LOG",
        "DEV",
        "WEB",
        "COM",
        "ORG",
        "NET",
        "API",
        "APP",
        "YES",
        "WHO",
        "WHY",
        "HOW",
        "ITS",
        "SEE",
        "SHE",
        "HIS",
        "HER",
        "HAD",
        "HAS",
        "WAS",
        "TOP",
        "BOT",
        "BAR",
        "BOX",
        "END",
        "DAY",
        "WAY",
        "KEY",
        "MAP",
        "ROW",
        "COL",
        "TAB",
        "DOC",
        "TRY",
        "ACT",
        "ADD",
        "ASK",
        "BUY",
        "PAY",
        "WIN",
        "USE",
        "OLD",
        "BAD",
        "FAR",
        "FEW",
        "BIT",
        "HOT",
        "RED",
        "RAW",
        "FIT",
        "HIT",
        "TAX",
        "JOB",
        "LAW",
        "CAR",
        "BAG",
        "AIR",
        "OIL",
        "GAS",  # Note: GAS is a listed stock; handled specifically when exact uppercase
        # Tech & web acronyms
        "PDF",
        "XLS",
        "CSV",
        "TXT",
        "JSON",
        "ZIP",
        "TAR",
        "RAR",
        "EXE",
        "SQL",
        "LLM",
        "RAG",
        "MCP",
        "SSE",
        "HTTP",
        "HTTPS",
        "REST",
        "SOAP",
        "HTML",
        "XML",
        "CSS",
        "URL",
        "URI",
        "SDK",
        "CLI",
        "SSH",
        "GIT",
        "NPM",
        "PIP",
        "DNS",
        "TCP",
        "UDP",
        "DOM",
        "IPC",
        "OSI",
        "TLS",
        "SSL",
        "CPU",
        "GPU",
        "RAM",
        "ROM",
        # Financial & trading acronyms
        "P/E",
        "P/B",
        "EPS",
        "ROE",
        "ROA",
        "EBIT",
        "EBITDA",
        "NAV",
        "CFO",
        "FCF",
        "DSO",
        "CAGR",
        "WACC",
        "NPL",
        "CASA",
        "LDR",
        "NIM",
        "CIR",
        "LLR",
        "ATC",
        "ATO",
        "VN30",
        "VNX50",
        "UPCOM",
        "HNX",
        "HSX",
        "HOSE",
        "VNINDEX",
        "GDKHQ",
        "ESOP",
        "CW",
        "ETF",
        "BCTC",
        "T+2",
        "T+1",
        "T+0",
        "KLGD",
        "GTGD",
        "LTM",
        "QOQ",
        "YOY",
        "ATH",
        "ATL",
        "DCH",
        "VOL",
        "AVG",
        "VCP",
    }
)


@lru_cache(maxsize=1)
def load_vn_tickers() -> dict[str, dict]:
    """Load Vietnamese stock tickers dictionary from data/vn_tickers.json."""
    if not DATA_FILE.exists():
        return {}
    with open(DATA_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("tickers", {})


@lru_cache(maxsize=1)
def get_vn_ticker_symbols() -> frozenset[str]:
    """Get the set of uppercase ticker codes."""
    tickers = load_vn_tickers()
    return frozenset(tickers.keys())


def is_vn_ticker(symbol: str) -> bool:
    """Check if symbol is a valid Vietnamese stock ticker, excluding stopwords."""
    sym_upper = symbol.strip().upper()
    if sym_upper in _VIET_STOCK_STOP_WORDS:
        # Special case: GAS is HOSE listed PetroVietnam Gas, but only allow if uppercase
        return bool(sym_upper == "GAS" and symbol == "GAS")
    return sym_upper in get_vn_ticker_symbols()
