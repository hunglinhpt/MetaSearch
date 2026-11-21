"""CoinGecko crypto market data (docs/32 §2).

Keyless public API (api.coingecko.com/api/v3) with a conservative 120s TTL
cache + single-flight so bursts cannot trip the anonymous rate limit. An
optional registered key upgrades quota via the demo/pro key headers.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from app.core.logging import get_logger
from app.core.singleflight import SingleFlight
from app.http import get_http_client
from app.retrieval.ext_data._base import DEFAULT_TIMEOUT_S, TtlCache


def _key_tag(api_key: str) -> str:
    """Deterministic short tag of the configured key (distinct keyed variants
    cache separately without storing the secret itself)."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:8] if api_key else ""

logger = get_logger(__name__)

_BASE_URL = "https://api.coingecko.com/api/v3"
_SOURCE_NAME = "CoinGecko"
_SOURCE_URL = "https://www.coingecko.com"

# CoinGecko ids are lowercase slugs ("bitcoin", "bnb", "binance-coin").
_ID_RE = re.compile(r"^[a-z0-9-]{1,64}$")
# Fiat quote currencies accepted by /simple/price.
_VS_RE = re.compile(r"^[a-z]{2,8}$")

_prices_cache = TtlCache(ttl_s=120.0, max_entries=64)
_global_cache = TtlCache(ttl_s=300.0, max_entries=8)
_prices_flight = SingleFlight()
_global_flight = SingleFlight()


def _key_headers(api_key: str) -> dict[str, str]:
    if not api_key:
        return {}
    # Demo keys ride the public host via header; pro keys use the pro host.
    if api_key.startswith("CG-"):
        return {"x-cg-pro-api-key": api_key}
    return {"x-cg-demo-api-key": api_key}


def sanitize_coin_ids(raw_ids: list[str]) -> list[str]:
    out: list[str] = []
    for raw in raw_ids[:25]:
        slug = raw.strip().lower()
        if slug and _ID_RE.fullmatch(slug) and slug not in out:
            out.append(slug)
    return out


def sanitize_vs_currency(raw: str) -> str:
    slug = raw.strip().lower()
    return slug if _VS_RE.fullmatch(slug) else "usd"


async def fetch_crypto_prices(
    coin_ids: list[str],
    *,
    vs_currency: str = "usd",
    api_key: str = "",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    client: Any = None,
) -> dict[str, Any] | None:
    """Prices + 24h/7d change + market caps for the given CoinGecko ids.

    Returns None when the upstream is dead (fail-open — the endpoint reports
    the outage, never fabricates)."""
    ids = sanitize_coin_ids(coin_ids)
    if not ids:
        return None
    vs = sanitize_vs_currency(vs_currency)
    # Cache key distinguishes keyless from keyed variants (different upstream
    # headers can yield different quota-driven responses) without storing keys.
    cache_key = f"prices:{','.join(ids)}:{vs}:{_key_tag(api_key)}"
    cached = _prices_cache.get(cache_key)
    if cached is not None:
        return dict(cached, cached=True)

    def _build() -> dict[str, Any]:
        params: dict[str, Any] = {
            "ids": ",".join(ids),
            "vs_currencies": vs,
            "include_24hr_change": "true",
            "include_24hr_vol": "true",
            "include_market_cap": "true",
            "include_last_updated_at": "true",
        }
        return params

    async def _load() -> dict[str, Any] | None:
        http_cli = client or get_http_client("search")
        try:
            resp = await http_cli.get(
                f"{_BASE_URL}/simple/price",
                params=_build(),
                headers=_key_headers(api_key),
                timeout=timeout_s,
            )
            if resp.status_code != 200:
                logger.warning("ext_coingecko_prices_failed", status=resp.status_code)
                return None
            data = resp.json()
        except Exception as exc:
            logger.warning("ext_coingecko_prices_error", error=type(exc).__name__)
            return None
        if not isinstance(data, dict) or not data:
            return None
        coins: list[dict[str, Any]] = []
        for coin_id in ids:
            node = data.get(coin_id)
            if not isinstance(node, dict):
                continue
            price = node.get(vs)
            if price is None:
                continue
            coins.append(
                {
                    "id": coin_id,
                    "price": _num(price),
                    "change_24h_pct": _num(node.get(f"{vs}_24h_change")),
                    "volume_24h": _num(node.get(f"{vs}_24h_vol")),
                    "market_cap": _num(node.get(f"{vs}_market_cap")),
                    "last_updated": _num(node.get("last_updated_at")),
                }
            )
        if not coins:
            return None
        return {
            "vs_currency": vs,
            "coins": coins,
            "source_name": _SOURCE_NAME,
            "source_url": _SOURCE_URL,
        }

    result = await _prices_flight.run(cache_key, _load)
    if result is None:
        return None
    _prices_cache.put(cache_key, result)
    return dict(result, cached=False)


async def fetch_crypto_global(
    *,
    api_key: str = "",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    client: Any = None,
) -> dict[str, Any] | None:
    """Global crypto market stats (total market cap, BTC dominance, active coins)."""
    cached = _global_cache.get("global")
    if cached is not None:
        return dict(cached, cached=True)

    async def _load() -> dict[str, Any] | None:
        http_cli = client or get_http_client("search")
        try:
            resp = await http_cli.get(
                f"{_BASE_URL}/global",
                headers=_key_headers(api_key),
                timeout=timeout_s,
            )
            if resp.status_code != 200:
                logger.warning("ext_coingecko_global_failed", status=resp.status_code)
                return None
            data = resp.json()
        except Exception as exc:
            logger.warning("ext_coingecko_global_error", error=type(exc).__name__)
            return None
        node = data.get("data") if isinstance(data, dict) else None
        if not isinstance(node, dict):
            return None

        def _pct(dotted: str) -> float | None:
            # market_cap_percentage is a nested dict — walk the dotted path.
            # CoinGecko /global already reports PERCENT values (57.76 = 57.76%);
            # verified live 2026-11-21 — do NOT multiply by 100 (unit test was
            # wrong about this; the live smoke caught it).
            raw: Any = node
            for part in dotted.split("."):
                if not isinstance(raw, dict):
                    return None
                raw = raw.get(part)
            try:
                return round(float(raw), 2) if raw is not None else None
            except (TypeError, ValueError):
                return None

        mcap = node.get("total_market_cap") or {}
        vol = node.get("total_volume") or {}
        return {
            "active_cryptocurrencies": node.get("active_cryptocurrencies"),
            "markets": node.get("markets"),
            "total_market_cap_usd": _num(mcap.get("usd")),
            "total_volume_24h_usd": _num(vol.get("usd")),
            "btc_dominance_pct": _pct("market_cap_percentage.btc"),
            "eth_dominance_pct": _pct("market_cap_percentage.eth"),
            "market_cap_change_24h_pct": _pct("market_cap_change_percentage_24h_usd"),
            "source_name": _SOURCE_NAME,
            "source_url": _SOURCE_URL,
        }

    result = await _global_flight.run("global", _load)
    if result is None:
        return None
    _global_cache.put("global", result)
    return dict(result, cached=False)


def _num(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return round(value, 8) if abs(value) < 1e15 else value
