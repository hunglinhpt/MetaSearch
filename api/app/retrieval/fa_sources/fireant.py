"""FireAnt company statements — direct source (docs/15 §5.5).

Internal API of the FireAnt platform (path verified via community docs):
``restv2.fireant.vn/api/companies/{SYM}/{income-statement,balance-sheet,cash-flow}``.
Requires a Bearer JWT obtained from fireant.vn login; the token is read from
settings (``FA_FIREANT_TOKEN``) and never hardcoded. Empty token disables the
source entirely. 401/403 raises ``FASourceError(RATE_LIMITED)`` so the breaker
opens instead of re-probing on every request.
"""

from __future__ import annotations

import asyncio
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
    ensure_symbol,
    pick,
    pick_float,
    to_vnd_bil,
)
from app.retrieval.health import Outcome

DEFAULT_BASE_URL = "https://restv2.fireant.vn/api/companies"

logger = logging.getLogger(__name__)

# Canonical field -> accepted payload key variants per statement type.
_INCOME_KEYS: dict[str, tuple[str, ...]] = {
    "revenue_vnd_bil": ("totalrevenue", "revenue"),
    "revenue_growth_pct": ("revenueyoy", "revenuegrowth"),
    "net_profit_vnd_bil": ("netprofit", "profitaftertax"),
    "profit_growth_pct": ("netprofityoy", "profitgrowth"),
}
_BALANCE_KEYS: dict[str, tuple[str, ...]] = {
    "total_assets_vnd_bil": ("totalassets",),
    "total_equity_vnd_bil": ("totalequity", "equity"),
    "total_debt_vnd_bil": ("totaldebt",),
}
_DATE_KEYS = ("fiscaldate", "reportdate", "enddate", "date", "createdat")

# statement slug -> canonical field map (zip order matches the parallel fetch).
_STATEMENT_KEYMAPS: dict[str, dict[str, tuple[str, ...]]] = {
    "income-statement": _INCOME_KEYS,
    "balance-sheet": _BALANCE_KEYS,
}

_token_logged = False


def _parse_grounded_date(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Full timestamp first, then the bare date part of "2025-09-30T10:00:00.123Z".
    for candidate in (text, text[:10]):
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(candidate, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
    return None


def _latest_record(records: list[dict]) -> dict:
    """Pick the record with the newest grounded date; ordering is not trusted."""
    best: dict = {}
    best_dt: datetime | None = None
    for rec in records:
        rec_dt = _latest_date([rec])
        if rec_dt and (best_dt is None or rec_dt > best_dt):
            best_dt = rec_dt
            best = rec
    return best or (records[0] if records else {})


def _latest_date(records: list[dict]) -> datetime | None:
    best: datetime | None = None
    for rec in records:
        for key in _DATE_KEYS:
            dt = _parse_grounded_date(pick(rec, key))  # case-insensitive (fiscalDate)
            if dt and (best is None or dt > best):
                best = dt
    return best


def _first_float(record: dict, variants: tuple[str, ...]) -> float | None:
    for v in variants:
        val = pick_float(record, v)
        if val is not None:
            return val
    return None


class FireAntFASource:
    """Direct FireAnt adapter — quarterly statements, Bearer JWT from settings."""

    source_id = "fireant"
    priority = 0.7

    def __init__(
        self,
        token: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token_override = token
        self.base_url = base_url.rstrip("/")
        self._client = client

    def _token(self) -> str:
        if self._token_override is not None:
            return self._token_override.strip()
        # Imported lazily so constructing the source never forces settings load
        # (keeps unit tests and CLI warm-up light).
        from app.config import get_settings

        return get_settings().fa_fireant_token.strip()

    async def fetch_ohlcv(self, symbol: str, *, timeout_s: float = 3.5) -> SourceOHLCV | None:
        return None  # statements-only source

    async def _fetch_statement(
        self, symbol: str, statement: str, token: str, timeout_s: float
    ) -> list[dict]:
        url = f"{self.base_url}/{symbol}/{statement}?quarterCount=4&years=0"
        headers = {**BROWSER_HEADERS, "Authorization": f"Bearer {token}"}
        try:
            client = self._client or get_http_client("search")
            resp = await client.get(url, headers=headers, follow_redirects=True, timeout=timeout_s)
        except httpx.TimeoutException as exc:
            raise FASourceError(self.source_id, "timeout", str(exc)) from exc
        except httpx.HTTPError as exc:
            raise FASourceError(self.source_id, "upstream_error", str(exc)) from exc

        if resp.status_code in (401, 403):
            # Auth faults trip the breaker instead of burning a probe per request.
            raise FASourceError(self.source_id, "rate_limited", f"auth_{resp.status_code}")
        if resp.status_code != 200:
            raise FASourceError(self.source_id, "upstream_error", f"status_{resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise FASourceError(self.source_id, "upstream_error", "bad_json") from exc
        if not isinstance(body, list):
            return []
        return [r for r in body if isinstance(r, dict)]

    async def fetch_financials(
        self, symbol: str, *, timeout_s: float = 3.5
    ) -> SourceFinancials | None:
        global _token_logged
        sym = ensure_symbol(symbol)
        if sym is None:
            return None
        token = self._token()
        if not token:
            if not _token_logged:
                _token_logged = True
                # Once per process: the owner needs to know FireAnt is off.
                logger.info("fa_fireant_disabled: FA_FIREANT_TOKEN is empty")
            return None

        fields: dict[str, float] = {}
        all_records: list[dict] = []
        statement_records = 0
        first_url = ""
        first_error: FASourceError | None = None

        # Both statements in parallel — never two sequential round-trips.
        results = await asyncio.gather(
            self._fetch_statement(sym, "income-statement", token, timeout_s),
            self._fetch_statement(sym, "balance-sheet", token, timeout_s),
            return_exceptions=True,
        )
        for statement, result in zip(("income-statement", "balance-sheet"), results, strict=True):
            if isinstance(result, FASourceError):
                if result.outcome == Outcome.RATE_LIMITED:
                    # Auth/rate faults are source-level (bad token, quota) —
                    # every statement would fail the same way. Propagate now so
                    # the breaker opens instead of silently degrading.
                    raise result
                # Upstream faults stay statement-scoped: keep whatever the
                # remaining statements ground. The first fault only propagates
                # when nothing at all was fetched, so the breaker still sees a
                # genuinely dead source.
                first_error = first_error or result
                logger.debug("fa_fireant_statement_failed: %s %s", sym, statement)
                continue
            if isinstance(result, BaseException):  # pragma: no cover — gather contract
                raise result
            records = result
            if not records:
                continue
            statement_records += len(records)
            if not first_url:
                first_url = f"{self.base_url}/{sym}/{statement}"
            all_records.extend(records)

            latest = _latest_record(records)
            keymap = _STATEMENT_KEYMAPS[statement]
            for canonical, variants in keymap.items():
                if canonical in fields:
                    continue
                val = _first_float(latest, variants)
                if val is None:
                    continue
                if canonical.endswith("_vnd_bil"):
                    converted = to_vnd_bil(val)
                    if converted is not None:
                        fields[canonical] = round(converted, 1)
                else:
                    fields[canonical] = val

        if not fields:
            if first_error is not None:
                raise first_error
            return None

        # Debt/Equity from the same grounded balance-sheet numbers (arithmetic).
        equity = fields.get("total_equity_vnd_bil")
        debt = fields.get("total_debt_vnd_bil")
        if equity is not None and debt is not None and equity > 0:
            fields["debt_to_equity"] = round(debt / equity, 3)

        return SourceFinancials(
            source_id=self.source_id,
            symbol=sym,
            fields=fields,
            as_of=_latest_date(all_records),
            source_url=first_url,
            statement_records=statement_records,
        )

    async def health_check(self, *, timeout_s: float = 2.0) -> bool:
        try:
            return await self.fetch_financials("TCB", timeout_s=timeout_s) is not None
        except FASourceError:
            return False
