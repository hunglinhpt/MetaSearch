"""SSI iBoard TradingView dchart — direct OHLCV source (docs/15 §5.1).

Same TradingView charting payload shape as VNDIRECT dchart:
``{"s": "ok", "t": [...], "o": [...], "h": [...], "l": [...], "c": [...], "v": [...]}``
Keyless, daily resolution. Unlike VNDIRECT we do NOT apply the x1000 price
correction — but that is a scale ASSUMPTION to re-verify, not a fact: docs/15
§10 proves the sibling TradingView feed (VNDIRECT, same payload convention)
serves /1000-scaled prices, which raises the prior that SSI does too. When the
SSI route is re-probed, verifying the price scale is a MANDATORY recorded step
(§10); until then the log-only ``fa_price_mismatch`` cross-check against
Simplize ``priceClose`` guards the final number whenever Simplize is up.

Live-probe note (2026-11-11, server egress): the historic
``iboard.ssi.com.vn/dchart/api/history`` route currently serves the iBoard SPA
HTML shell instead of TradingView JSON. The adapter therefore fails with
``FASourceError(bad_json)`` and its breaker opens — graceful, by design. When
the endpoint is re-verified, only ``DEFAULT_BASE_URL`` needs updating.
"""

from __future__ import annotations

import logging
import time
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
    to_float,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://iboard.ssi.com.vn/dchart/api/history"


class SSIFASource:
    """Direct SSI iBoard adapter — OHLCV only (fetch_financials is always None)."""

    source_id = "ssi"
    priority = 0.9
    can_financials = False
    can_ohlcv = True

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
        check_payload_size(resp, self.source_id)
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
        o_list = data.get("o") or []
        h_list = data.get("h") or []
        l_list = data.get("l") or []
        v_list = data.get("v") or []

        if len(c_list) < 5:
            return None

        # STRICT arrays: a missing/mismatched o/h/l/v is schema drift — refuse
        # the series rather than fabricating closes-for-opens or zero volumes
        # (zero volumes would silently poison OBV/MFI and pattern detection).
        series: dict[str, list[float]] = {}
        for name, raw in (
            ("opens", o_list),
            ("highs", h_list),
            ("lows", l_list),
            ("volumes", v_list),
        ):
            if len(raw) != len(c_list):
                logger.debug("ssi_series_schema_drift: %s field=%s", sym, name)
                return None
            values = [to_float(x) for x in raw]
            if any(v is None for v in values):  # NaN/Infinity/str — json accepts NaN
                raise FASourceError(self.source_id, "upstream_error", f"non_finite_{name}")
            series[name] = values
        closes = [to_float(x) for x in c_list]
        if any(v is None for v in closes):
            raise FASourceError(self.source_id, "upstream_error", "non_finite_closes")
        try:
            times = [int(t) for t in t_list]
            last_ts = times[-1] if times else None
        except (TypeError, ValueError) as exc:
            raise FASourceError(self.source_id, "upstream_error", "bad_series") from exc
        if len(times) != len(closes):
            # A timestamp array that does not align with the candles would put
            # every bar on the wrong day in charts — refuse instead of guessing.
            logger.debug("ssi_series_schema_drift: %s field=times", sym)
            return None
        if any(times[i] >= times[i + 1] for i in range(len(times) - 1)):
            # Charts require strictly ascending time.
            logger.debug("ssi_series_schema_drift: %s times_not_ascending", sym)
            return None

        if any(x < 0 for x in closes) or any(x < 0 for x in series["volumes"]):
            raise FASourceError(self.source_id, "upstream_error", "negative_values")

        return SourceOHLCV(
            source_id=self.source_id,
            opens=series["opens"],
            highs=series["highs"],
            lows=series["lows"],
            closes=closes,
            volumes=series["volumes"],
            times=times,
            last_ts=last_ts,
            source_url=url,
        )
