"""TCBS stock-insight financial ratios — direct source (docs/15 §5.4).

Internal API of the TCBS web platform (no official docs). Keyless. The adapter
maps only KNOWN key variants onto canonical field names and silently skips
anything unrecognized — when TCBS renames a field we lose one metric, never
invent one. Schema re-probe then extends ``_RATIO_KEYS`` by one line.

Live-probe note (2026-11-11, server egress): ``api.tcbs.com.vn`` did not
resolve DNS and the ecosystem alternate ``apipubaws.tcbs.com.vn`` answers a
Cloudflare bot challenge to non-browser clients. The adapter stays direct and
keyless; a challenge/HTML body fails as ``bad_json`` and the breaker opens.
Unit gates below drop any value that is implausible for the canonical field
(e.g. a ratio-valued ROE or a market cap in the wrong scale) with a
``fa_suspect_unit`` warning instead of serving a silently mis-scaled number.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.http import get_http_client
from app.retrieval.fa_sources.base import (
    BROWSER_HEADERS,
    FASourceError,
    SourceFinancials,
    SourceOHLCV,
    check_payload_size,
    ensure_symbol,
    pick,
    pick_float,
)

logger = logging.getLogger(__name__)

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

# Period-end keys anchor as_of (the data's own date). Publish-side keys
# (reportdate/createdat) are a DIFFERENT claim and map to published_at only.
_PERIOD_KEYS = ("enddate", "date", "period", "fiscaldate")
_PUBLISH_KEYS = ("reportdate", "createdat")

# Ratio values are taken payload-native (VN sources are VND-native; EPS in VND,
# percentages in percent). Only unit-scale arithmetic (market cap -> billions)
# is applied, plus the plausibility gates below — no unit is ever guessed.
_PCT_FIELD_BOUNDS = {"roe_pct": 200.0, "roa_pct": 200.0}
_MARKET_CAP_VND_BIL_BAND = (5.0, 3_000_000.0)  # listed VN issuers, tỷ đ
_EPS_VND_BOUND = 1_000_000.0


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


def _latest_by_keys(records: list[dict[str, Any]], keys: tuple[str, ...]) -> datetime | None:
    best: datetime | None = None
    for rec in records:
        for key in keys:
            dt = _parse_date(pick(rec, key))  # case-insensitive
            if dt and (best is None or dt > best):
                best = dt
    return best


def _latest_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Record with the newest PERIOD-END date; ordering is not trusted."""
    best: dict[str, Any] | None = None
    best_dt: datetime | None = None
    for rec in records:
        for key in _PERIOD_KEYS:
            dt = _parse_date(pick(rec, key))
            if dt and (best_dt is None or dt > best_dt):
                best_dt = dt
                best = rec
                break
    if best is None and records:
        best = records[-1]
    return best or {}


def _drop_implausible(fields: dict[str, float], sym: str) -> None:
    """Unit-detector gates: drop + warn instead of serving a mis-scaled value."""
    for name, bound in _PCT_FIELD_BOUNDS.items():
        val = fields.get(name)
        if val is not None and abs(val) > bound:
            logger.warning("fa_suspect_unit: %s %s=%s beyond ±%s", sym, name, val, bound)
            del fields[name]
    mc = fields.get("market_cap_vnd_bil")
    if mc is not None and not (_MARKET_CAP_VND_BIL_BAND[0] <= mc <= _MARKET_CAP_VND_BIL_BAND[1]):
        logger.warning(
            "fa_suspect_unit: %s market_cap_vnd_bil=%s outside %s",
            sym,
            mc,
            _MARKET_CAP_VND_BIL_BAND,
        )
        del fields["market_cap_vnd_bil"]
    eps = fields.get("eps_vnd")
    if eps is not None and abs(eps) > _EPS_VND_BOUND:
        logger.warning("fa_suspect_unit: %s eps_vnd=%s beyond ±%s", sym, eps, _EPS_VND_BOUND)
        del fields["eps_vnd"]


class TCBSFASource:
    """Direct TCBS adapter — financial ratios, keyless."""

    source_id = "tcbs"
    priority = 0.8
    can_financials = True
    can_ohlcv = False

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
        check_payload_size(resp, self.source_id)
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
        _drop_implausible(fields, sym)
        if not fields:
            return None

        return SourceFinancials(
            source_id=self.source_id,
            symbol=sym,
            fields=fields,
            as_of=_latest_by_keys(records, _PERIOD_KEYS),
            published_at=_latest_by_keys(records, _PUBLISH_KEYS),
            source_url=url,
            statement_records=0,
        )
