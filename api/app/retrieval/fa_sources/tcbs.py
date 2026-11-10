"""TCBS stock-insight financial ratios — direct source (docs/15 §5.4).

Internal API of the TCBS web platform (no official docs). Keyless. The adapter
maps only KNOWN key variants onto canonical field names and silently skips
anything unrecognized — when TCBS renames a field we lose one metric, never
invent one. Schema re-probe then extends ``_RATIO_KEYS`` by one line.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.http import get_http_client
from app.retrieval.fa_sources.base import (
    BROWSER_HEADERS,
    FASourceError,
    SourceFinancials,
    SourceOHLCV,
    ensure_symbol,
    pick,
    pick_float,
)

DEFAULT_BASE_URL = "https://api.tcbs.com.vn/stock-insight/v1/stock"

# Canonical field -> accepted payload key variants (case-insensitive).
_RATIO_KEYS: dict[str, tuple[str, ...]] = {
    "pe": ("pe", "pricetoearning", "priceearning"),
    "pb": ("pb", "pricetobook", "pricebook"),
    "roe_pct": ("roe", "roea", "roeavg"),
    "roa_pct": ("roa", "roaa", "roaavg"),
    "eps_vnd": ("eps", "epstrailing12m", "earningpershare"),
    "debt_to_equity": ("debttoequity", "de", "debtonequity"),
    "market_cap_vnd_bil": ("marketcap",),
    "revenue_growth_pct": ("revenuegrowth", "salesgrowth", "revenuegrowth1y"),
    "profit_growth_pct": ("profitgrowth", "netprofitgrowth", "earninggrowth"),
}

# Ratio values are taken payload-native (VN sources are VND-native; EPS in VND,
# percentages in percent). Only unit-scale arithmetic (market cap -> billions)
# is applied — no unit is ever guessed.

# Candidate keys carrying a grounded period/report date.
_DATE_KEYS = ("date", "createdat", "calendardate", "period", "reportdate", "enddate")


def _extract_records(body: Any) -> list[dict[str, Any]]:
    """Find the ratio record list defensively (known wrapper or first dict list)."""
    if isinstance(body, dict):
        inner = body.get("data", body)
        if isinstance(inner, dict):
            inner = inner.get("data", inner)
        if isinstance(inner, list):
            return [r for r in inner if isinstance(r, dict)]
        if isinstance(inner, dict):
            for val in inner.values():
                if isinstance(val, list) and val and isinstance(val[0], dict):
                    return [r for r in val if isinstance(r, dict)]
    elif isinstance(body, list):
        return [r for r in body if isinstance(r, dict)]
    return []


def _parse_date(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Full string first, then the bare date part of "2024-12-31T00:00:00.000".
    for candidate in (text, text[:10]):
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y"):
            try:
                return datetime.strptime(candidate, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
    return None


def _latest_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Most recent record by a grounded date key; fall back to the last row."""
    best: dict[str, Any] | None = None
    best_dt: datetime | None = None
    for rec in records:
        for key in _DATE_KEYS:
            dt = _parse_date(pick(rec, key))  # case-insensitive
            if dt and (best_dt is None or dt > best_dt):
                best_dt = dt
                best = rec
                break
    if best is None and records:
        best = records[-1]
    return best or {}


def _latest_date(records: list[dict[str, Any]]) -> datetime | None:
    best: datetime | None = None
    for rec in records:
        for key in _DATE_KEYS:
            dt = _parse_date(pick(rec, key))  # case-insensitive
            if dt and (best is None or dt > best):
                best = dt
    return best


class TCBSFASource:
    """Direct TCBS adapter — financial ratios, keyless."""

    source_id = "tcbs"
    priority = 0.8

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client

    def _url(self, symbol: str) -> str:
        return f"{self.base_url}/{symbol}/financial-ratio?period=1&size=100&isAll=true"

    async def fetch_ohlcv(self, symbol: str, *, timeout_s: float = 3.5) -> SourceOHLCV | None:
        return None  # ratios-only source

    async def fetch_financials(
        self, symbol: str, *, timeout_s: float = 3.5
    ) -> SourceFinancials | None:
        sym = ensure_symbol(symbol)
        if sym is None:
            return None
        url = self._url(sym)
        try:
            client = self._client or get_http_client("search")
            resp = await client.get(
                url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=timeout_s
            )
        except httpx.TimeoutException as exc:
            raise FASourceError(self.source_id, "timeout", str(exc)) from exc
        except httpx.HTTPError as exc:
            raise FASourceError(self.source_id, "upstream_error", str(exc)) from exc

        if resp.status_code != 200:
            raise FASourceError(self.source_id, "upstream_error", f"status_{resp.status_code}")
        try:
            body: Any = resp.json()
        except ValueError as exc:
            raise FASourceError(self.source_id, "upstream_error", "bad_json") from exc

        records = _extract_records(body)
        if not records:
            return None  # healthy response, no coverage for this symbol

        latest = _latest_record(records)
        fields: dict[str, float] = {}
        for canonical, variants in _RATIO_KEYS.items():
            raw = pick_float(latest, *variants)
            if raw is not None:
                fields[canonical] = raw
        if not fields:
            return None  # schema drifted entirely — emit nothing rather than guess

        # TCBS market cap is VND-native; converting to billions is arithmetic.
        if "market_cap_vnd_bil" in fields:
            fields["market_cap_vnd_bil"] = round(fields["market_cap_vnd_bil"] / 1e9, 1)

        return SourceFinancials(
            source_id=self.source_id,
            symbol=sym,
            fields=fields,
            as_of=_latest_date(records),
            source_url=url,
            statement_records=0,
        )

    async def health_check(self, *, timeout_s: float = 2.0) -> bool:
        try:
            return await self.fetch_financials("TCB", timeout_s=timeout_s) is not None
        except FASourceError:
            return False
