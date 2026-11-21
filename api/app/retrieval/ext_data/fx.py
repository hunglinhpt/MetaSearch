"""FX rates (docs/32 §3): ExchangeRate-API + Frankfurter (ECB).

Both public tiers are keyless: ``open.er-api.com`` covers 160+ currencies
(including VND) updated daily; Frankfurter (ECB reference rates) is the
authoritative source for its currency set. The endpoint fans out to BOTH,
merges (Frankfurter wins ties — official ECB prints), and reports per-currency
provenance. An optional ``EXCHANGERATE_API_KEY`` upgrades the ER-API leg to
the keyed host (fresh intraday updates on paid plans).
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger
from app.core.singleflight import SingleFlight
from app.http import get_http_client
from app.retrieval.ext_data._base import DEFAULT_TIMEOUT_S, TtlCache
from app.retrieval.ext_data.crypto import _key_tag

logger = get_logger(__name__)

_ER_OPEN_URL = "https://open.er-api.com/v6/latest/{base}"
_ER_KEYED_URL = "https://v6.exchangerate-api.com/v6/{key}/latest/{base}"
_FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest"

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_MAX_SYMBOLS = 20

_fx_cache = TtlCache(ttl_s=1800.0, max_entries=32)
_fx_flight = SingleFlight()


def sanitize_currency(raw: str) -> str | None:
    code = raw.strip().upper()
    return code if _CURRENCY_RE.fullmatch(code) else None


def sanitize_symbols(raw_symbols: list[str]) -> list[str]:
    out: list[str] = []
    for raw in raw_symbols[:_MAX_SYMBOLS]:
        code = sanitize_currency(raw)
        if code and code not in out:
            out.append(code)
    return out


async def fetch_fx_rates(
    base: str,
    symbols: list[str],
    *,
    exchangerate_api_key: str = "",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    client: Any = None,
) -> dict[str, Any] | None:
    """Rates of ``base`` → ``symbols`` from Frankfurter + ER-API (merged).

    Returns None only when BOTH sources fail (fail-open merge otherwise)."""
    base_code = sanitize_currency(base)
    if base_code is None:
        return None
    wanted = sanitize_symbols(symbols)
    cache_key = f"fx:{base_code}:{','.join(wanted)}:{_key_tag(exchangerate_api_key)}"
    cached = _fx_cache.get(cache_key)
    if cached is not None:
        return dict(cached, cached=True)

    async def _load() -> dict[str, Any] | None:
        er_task = _fetch_er_api(
            base_code, exchangerate_api_key=exchangerate_api_key, timeout_s=timeout_s, client=client
        )
        ff_task = _fetch_frankfurter(base_code, timeout_s=timeout_s, client=client)
        er_data, ff_data = await asyncio.gather(er_task, ff_task)
        if er_data is None and ff_data is None:
            return None

        rates: dict[str, dict[str, Any]] = {}
        sources_used: list[str] = []
        # Frankfurter first (official ECB prints win ties).
        if ff_data is not None:
            sources_used.append("Frankfurter (ECB)")
            for code, rate in ff_data["rates"].items():
                if not wanted or code in wanted:
                    rates[code] = {
                        "rate": rate,
                        "source_name": "Frankfurter (ECB)",
                        "source_url": "https://frankfurter.dev",
                        "as_of": ff_data["date"],
                    }
        if er_data is not None:
            sources_used.append("ExchangeRate-API")
            for code, rate in er_data["rates"].items():
                if not wanted or code in wanted:
                    if code in rates:
                        continue  # ECB print kept
                    rates[code] = {
                        "rate": rate,
                        "source_name": "ExchangeRate-API",
                        "source_url": "https://www.exchangerate-api.com",
                        "as_of": er_data["as_of"],
                    }
        if not rates:
            return None
        return {
            "base": base_code,
            "rates": dict(sorted(rates.items())),
            "sources_used": sources_used,
            "requested_symbols": wanted,
            "as_of": max((r.get("as_of") or "" for r in rates.values()), default=""),
        }

    result = await _fx_flight.run(cache_key, _load)
    if result is None:
        return None
    _fx_cache.put(cache_key, result)
    return dict(result, cached=False)


async def _fetch_er_api(
    base: str,
    *,
    exchangerate_api_key: str = "",
    timeout_s: float,
    client: Any = None,
) -> dict[str, Any] | None:
    url = (
        _ER_KEYED_URL.format(key=exchangerate_api_key, base=base)
        if exchangerate_api_key
        else _ER_OPEN_URL.format(base=base)
    )
    try:
        http_cli = client or get_http_client("search")
        resp = await http_cli.get(url, timeout=timeout_s)
        if resp.status_code != 200:
            logger.warning("ext_erapi_failed", status=resp.status_code)
            return None
        data = resp.json()
    except Exception as exc:
        logger.warning("ext_erapi_error", error=type(exc).__name__)
        return None
    if not isinstance(data, dict) or data.get("result") not in ("success", None):
        return None
    rates = data.get("rates")
    if not isinstance(rates, dict):
        return None
    parsed = _parse_rates(rates)
    if parsed is None:
        return None
    return {
        "rates": parsed,
        "as_of": str(data.get("time_last_update_utc") or _today()),
    }


async def _fetch_frankfurter(
    base: str,
    *,
    timeout_s: float,
    client: Any = None,
) -> dict[str, Any] | None:
    try:
        http_cli = client or get_http_client("search")
        resp = await http_cli.get(_FRANKFURTER_URL, params={"base": base}, timeout=timeout_s)
        if resp.status_code != 200:
            logger.warning("ext_frankfurter_failed", status=resp.status_code)
            return None
        data = resp.json()
    except Exception as exc:
        logger.warning("ext_frankfurter_error", error=type(exc).__name__)
        return None
    if not isinstance(data, dict):
        return None
    parsed = _parse_rates(data.get("rates") or {})
    if parsed is None:
        return None
    return {"rates": parsed, "date": str(data.get("date") or "")}


def _parse_rates(rates: dict[str, Any]) -> dict[str, float] | None:
    out: dict[str, float] = {}
    for code, value in rates.items():
        if not _CURRENCY_RE.fullmatch(str(code)):
            continue
        try:
            num = float(value)
        except (TypeError, ValueError):
            continue
        if num <= 0 or num != num or num in (float("inf"), float("-inf")):
            continue
        out[str(code)] = round(num, 8)
    return out or None


def _today() -> str:
    return datetime.now(UTC).date().isoformat()
