"""SSI iBoard TradingView dchart — direct OHLCV source (docs/15 §5.1).

Same TradingView charting payload shape as VNDIRECT dchart:
``{"s": "ok", "t": [...], "o": [...], "h": [...], "l": [...], "c": [...], "v": [...]}``
Keyless, daily resolution. Unlike VNDIRECT we do NOT apply the x1000 price
correction: there is no evidence SSI serves scaled prices, and "correcting"
a feed we have not verified would be inventing data.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.http import get_http_client
from app.retrieval.fa_sources.base import (
    BROWSER_HEADERS,
    FASourceError,
    SourceFinancials,
    SourceOHLCV,
    ensure_symbol,
)

DEFAULT_BASE_URL = "https://iboard.ssi.com.vn/dchart/api/history"


class SSIFASource:
    """Direct SSI iBoard adapter — OHLCV only (fetch_financials is always None)."""

    source_id = "ssi"
    priority = 0.9

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client

    async def fetch_financials(
        self, symbol: str, *, timeout_s: float = 3.5
    ) -> SourceFinancials | None:
        return None  # OHLCV-only source

    async def fetch_ohlcv(self, symbol: str, *, timeout_s: float = 3.5) -> SourceOHLCV | None:
        sym = ensure_symbol(symbol)
        if sym is None:
            return None
        now_ts = int(time.time())
        from_ts = now_ts - 240 * 86400  # same 240-day window as the VNDIRECT path
        url = f"{self.base_url}?resolution=D&symbol={sym}&from={from_ts}&to={now_ts}"

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
            data: Any = resp.json()
        except ValueError as exc:
            raise FASourceError(self.source_id, "upstream_error", "bad_json") from exc

        if not isinstance(data, dict) or data.get("s") != "ok":
            # TradingView protocol: "s" != "ok" means "no data / error" — that is
            # a healthy answer, not a fault.
            return None

        t_list = data.get("t") or []
        c_list = data.get("c") or []
        o_list = data.get("o") or c_list
        h_list = data.get("h") or c_list
        l_list = data.get("l") or c_list
        v_list = data.get("v") or [0.0] * len(c_list)

        if len(c_list) < 5:
            return None

        try:
            closes = [float(x) for x in c_list]
            opens = [float(x) for x in o_list] if len(o_list) == len(c_list) else list(closes)
            highs = [float(x) for x in h_list] if len(h_list) == len(c_list) else list(closes)
            lows = [float(x) for x in l_list] if len(l_list) == len(c_list) else list(closes)
            volumes = (
                [float(x) for x in v_list] if len(v_list) == len(c_list) else [0.0] * len(c_list)
            )
            last_ts = int(t_list[-1]) if t_list else None
        except (TypeError, ValueError) as exc:
            raise FASourceError(self.source_id, "upstream_error", "bad_series") from exc

        if any(x < 0 for x in closes) or any(x < 0 for x in volumes):
            raise FASourceError(self.source_id, "upstream_error", "negative_values")

        return SourceOHLCV(
            source_id=self.source_id,
            opens=opens,
            highs=highs,
            lows=lows,
            closes=closes,
            volumes=volumes,
            last_ts=last_ts,
            source_url=url,
        )

    async def health_check(self, *, timeout_s: float = 2.0) -> bool:
        try:
            return await self.fetch_ohlcv("VNINDEX", timeout_s=timeout_s) is not None
        except FASourceError:
            return False
