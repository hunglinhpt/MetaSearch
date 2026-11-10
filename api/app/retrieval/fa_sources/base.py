"""FA source contracts: normalized payloads shared by every direct-source adapter.

Multi-source FA plan (docs/15):
- Each source is a self-contained module calling its own endpoint directly over
  the shared httpx pools. No third-party wrapper (vnstock/vnquant) in between —
  a wrapper would couple every source to one dependency and hide real endpoints.
- A source NEVER invents a value: unknown schema keys are skipped, dates come
  from the payload only (never ``datetime.now()``), missing data is ``None``.
- Provenance (source_id, source_url, grounded as_of) travels with every payload
  so the aggregator can tag each merged field.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from app.retrieval.health import Outcome

# Symbols are interpolated into hardcoded API URL paths; only plain codes pass.
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,15}$")


def ensure_symbol(symbol: str) -> str | None:
    """Normalized ticker (uppercase) or ``None`` when it is not a plain code.

    Defense in depth beyond the adapter's own registry whitelist: reject
    anything that could mutate the URL path before it reaches an f-string.
    """
    sym = (symbol or "").strip().upper()
    return sym if _SYMBOL_RE.match(sym) else None


# Canonical normalized field names every adapter maps into. A source only emits
# the subset it can ground; the aggregator tracks the rest as missing.
CANONICAL_FA_FIELDS: tuple[str, ...] = (
    "market_cap_vnd_bil",
    "pe",
    "pb",
    "eps_vnd",
    "roe_pct",
    "roa_pct",
    "debt_to_equity",
    "revenue_vnd_bil",
    "net_profit_vnd_bil",
    "revenue_growth_pct",
    "profit_growth_pct",
    "free_float_pct",
    "dividend_yield_pct",
    # Raw balance-sheet anchors (FireAnt) — kept for provenance even though the
    # viet_stock renderers only consume debt_to_equity derived from them.
    "total_assets_vnd_bil",
    "total_equity_vnd_bil",
    "total_debt_vnd_bil",
)

# Hard byte cap for a single source response (repo convention: 5MB).
MAX_RESPONSE_BYTES = 5 * 1024 * 1024

# Browser-like headers for internal/web-platform APIs that reject bot agents.
BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


@dataclass(slots=True)
class SourceFinancials:
    """Normalized fundamental snapshot from exactly one source."""

    source_id: str
    symbol: str
    # Canonical field name -> grounded value. Only fields present in the
    # source's response appear here.
    fields: dict[str, float] = field(default_factory=dict)
    # Period-end date from the payload (fiscalDate / enddate) — the data's own
    # anchor, NOT a publication time.
    as_of: datetime | None = None
    # Publication-side date when the payload carries one (reportDate /
    # createdAt). Period end and publish date are different provenance claims
    # and are never merged into one field.
    published_at: datetime | None = None
    source_url: str = ""
    # How many statement records back the numbers (0 = ratios only).
    statement_records: int = 0
    # True when the source served PARTIAL data (one statement failed while the
    # other succeeded). Logged by the aggregator; breaker stays untouched.
    partial_fault: bool = False


@dataclass(slots=True)
class SourceOHLCV:
    """Normalized OHLCV series from exactly one source."""

    source_id: str
    opens: list[float] = field(default_factory=list)
    highs: list[float] = field(default_factory=list)
    lows: list[float] = field(default_factory=list)
    closes: list[float] = field(default_factory=list)
    volumes: list[float] = field(default_factory=list)
    last_ts: int | None = None
    source_url: str = ""

    @property
    def n(self) -> int:
        return len(self.closes)


class FASourceError(Exception):
    """A source-level fault the aggregator records against that source's breaker.

    Raised for HTTP >= 400, network failures and unparseable bodies. A healthy
    response that simply has no data for the symbol returns ``None`` instead
    (Outcome.EMPTY — not a fault, breaker stays closed).
    """

    def __init__(self, source_id: str, outcome: Outcome | str, detail: str = "") -> None:
        self.source_id = source_id
        self.outcome = Outcome(outcome)  # accept plain strings from call sites
        self.detail = detail
        super().__init__(f"[{source_id}] {self.outcome.value}: {detail}")


class FASource(Protocol):
    """Contract every direct FA source adapter implements.

    ``can_*`` capability flags let the aggregator fan out ONLY to sources that
    actually serve the requested leg: stub methods returning ``None`` without
    network I/O must never be probed or recorded — their vacuous EMPTY outcomes
    would reset the breaker streaks of genuinely-failing endpoints.
    """

    source_id: str
    priority: float  # 1.0 = highest; lower loses merge ties
    can_financials: bool
    can_ohlcv: bool

    async def fetch_financials(
        self, symbol: str, *, timeout_s: float = 3.5
    ) -> SourceFinancials | None: ...

    async def fetch_ohlcv(self, symbol: str, *, timeout_s: float = 3.5) -> SourceOHLCV | None: ...


def check_payload_size(resp: Any, source_id: str) -> None:
    """Reject oversized bodies before ``resp.json()`` can stall the event loop."""
    size = len(getattr(resp, "content", b"") or b"")
    if size > MAX_RESPONSE_BYTES:
        raise FASourceError(source_id, "upstream_error", f"payload_too_large_{size}_bytes")


def pick(record: Mapping[str, Any], *keys: str) -> Any:
    """First present, non-None value among candidate keys (case-insensitive).

    Sources evolve their field names; adapters list the known variants and
    anything unmatched is skipped — never guessed.
    """
    lowered = {str(k).lower(): v for k, v in record.items()}
    for key in keys:
        val = lowered.get(key.lower())
        if val is not None:
            return val
    return None


def to_float(value: Any) -> float | None:
    """Best-effort finite float; rejects bool/NaN/inf/strings that don't parse."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return num


def pick_float(record: Mapping[str, Any], *keys: str) -> float | None:
    return to_float(pick(record, *keys))


def to_vnd_bil(value: Any) -> float | None:
    """Convert a payload-native VND amount to billions of VND (pure arithmetic)."""
    num = to_float(value)
    return None if num is None else num / 1e9
