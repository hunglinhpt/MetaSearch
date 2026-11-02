"""Refresh Vietnamese listed tickers catalog from VNDIRECT Finfo API.

Saves real listed tickers (HOSE, HNX, UPCOM) to app/retrieval/data/vn_tickers.json.
Enforces >= 1,500 active listed symbols.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

FINFO_URL = "https://api-finfo.vndirect.com.vn/v4/stocks?q=type:STOCK~status:LISTED&size=3000"
OUTPUT_PATH = (
    Path(__file__).resolve().parent.parent / "app" / "retrieval" / "data" / "vn_tickers.json"
)


def fetch_vn_tickers() -> dict:
    req = Request(FINFO_URL, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    raw_items = payload.get("data", [])
    tickers: dict[str, dict[str, str]] = {}

    for item in raw_items:
        code = str(item.get("code", "")).strip().upper()
        if len(code) == 3 and code.isalnum():
            tickers[code] = {
                "symbol": code,
                "company_name": str(item.get("companyName", "") or ""),
                "short_name": str(item.get("shortName", "") or ""),
                "exchange": str(item.get("floor", "") or ""),
                "isin": str(item.get("isin", "") or ""),
            }

    if len(tickers) < 1500:
        raise RuntimeError(f"Expected >= 1500 tickers, got only {len(tickers)}")

    catalog = {
        "as_of": datetime.now(UTC).date().isoformat(),
        "source": "VNDIRECT Finfo API (v4/stocks)",
        "source_url": FINFO_URL,
        "total_tickers": len(tickers),
        "tickers": tickers,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)

    print(f"Successfully refreshed {len(tickers)} tickers to {OUTPUT_PATH}")
    return catalog


if __name__ == "__main__":
    fetch_vn_tickers()
