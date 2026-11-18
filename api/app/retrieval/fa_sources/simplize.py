"""Simplize FA source — keyless VN fundamentals (docs/29 ISSUE-3).

TCBS is DNS-dead from project egress and FireAnt needs a JWT that defaults
empty, so `/v1/fa/compare` degraded to empty payloads for valid tickers.
Simplize's company-summary API is the proven-live keyless fundamental feed
(docs/15 §10; already consumed by the viet_stock adapter and the sentiment
quant fallback). This adapter exposes that feed as a first-class FA source so
the aggregator merges REAL fundamentals again.

Coverage note (docs/15 §10): Simplize serves listed VN tickers; small caps
may 404 — a healthy no-coverage response returns ``None`` (Outcome.EMPTY),
never a fabricated number.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from app.core.logging import get_logger
from app.http import get_http_client
from app.retrieval.fa_sources.base import (
    BROWSER_HEADERS,
    SourceFinancials,
    check_payload_size,
    ensure_symbol,
)

logger = get_logger(__name__)


class SimplizeFASource:
    """Keyless Simplize company-summary fundamentals (pe/pb/roe/mcap)."""

    source_id = "simplize"
    priority = 0.6  # below FireAnt (0.7); above nothing else with financials
    can_financials = True
    can_ohlcv = False

    def __init__(
        self,
        *,
        base_url: str = "https://api.simplize.vn/api/company/summary",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url
        self._client = client

    async def fetch_financials(
        self, symbol: str, *, timeout_s: float = 3.5
    ) -> SourceFinancials | None:
        sym = ensure_symbol(symbol)
        if sym is None:
            return None
        url = f"{self._base_url}/{quote(sym, safe='')}"
        try:
            client = self._client or get_http_client("search")
            resp = await client.get(
                url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=timeout_s
            )
        except httpx.TimeoutException as exc:
            from app.retrieval.fa_sources.base import FASourceError

            raise FASourceError(self.source_id, "timeout", str(exc)) from exc
        except httpx.HTTPError as exc:
            from app.retrieval.fa_sources.base import FASourceError

            raise FASourceError(self.source_id, "upstream_error", str(exc)) from exc

        from app.retrieval.fa_sources.base import FASourceError

        if resp.status_code == 404:
            return None  # small-cap / unlisted ticker without Simplize coverage
        if resp.status_code != 200:
            raise FASourceError(self.source_id, "upstream_error", f"status_{resp.status_code}")
        check_payload_size(resp, self.source_id)
        try:
            body: dict = resp.json()
        except ValueError as exc:
            raise FASourceError(self.source_id, "upstream_error", "bad_json") from exc

        if not isinstance(body, dict) or body.get("status") not in (None, 200):
            return None
        data = body.get("data")
        if not isinstance(data, dict):
            return None  # healthy response, no coverage for this symbol

        fields: dict[str, float] = {}

        def _num(key: str) -> float | None:
            value = data.get(key)
            if isinstance(value, bool):
                return None
            try:
                val = float(value) if value is not None else None
            except (TypeError, ValueError):
                return None
            return val if val is not None and math.isfinite(val) else None

        pe = _num("peRatio")
        if pe is not None and pe > 0:
            fields["pe"] = round(pe, 2)
        pb = _num("pbRatio")
        if pb is not None and pb > 0:
            fields["pb"] = round(pb, 2)
        # Live Simplize summary API returns `roe` and `roa` already in percent (1.19 = 1.19%).
        roe = _num("roe")
        if roe is not None:
            fields["roe_pct"] = round(roe, 2)
        roa = _num("roa")
        if roa is not None:
            fields["roa_pct"] = round(roa, 2)
        mc_raw = _num("marketCap")
        if mc_raw is not None and mc_raw > 0:
            # Simplize reports VND; the canonical field is billions VND.
            fields["market_cap_vnd_bil"] = round(mc_raw / 1e9, 2)
        eps = _num("epsRatio")
        if eps is None:
            eps = _num("eps")
        if eps is not None and eps > 0:
            fields["eps_vnd"] = round(eps, 2)

        if not fields:
            return None  # payload shape without usable ratios

        # Simplize carries no period anchor in the summary endpoint — the
        # fetch timestamp is the honest as_of for a "current snapshot".
        return SourceFinancials(
            source_id=self.source_id,
            symbol=sym,
            fields=fields,
            as_of=datetime.now(UTC),
            source_url=f"https://simplize.vn/co-phieu/{quote(sym, safe='')}",
            statement_records=0,
            partial_fault=False,
        )

    async def fetch_ohlcv(self, symbol: str, *, timeout_s: float = 3.5):
        return None  # can_ohlcv False — never probed
