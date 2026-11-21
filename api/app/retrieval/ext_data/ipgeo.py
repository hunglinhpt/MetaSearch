"""IP geolocation (docs/32 §7): ipwho.is keyless primary + IPinfo upgrade.

ipwho.is requires no registration (free for non-commercial use) so the
surface works out of the box; an optional ``IPINFO_TOKEN`` adds an IPinfo
block (city/org/ASN precision).
"""

from __future__ import annotations

import ipaddress
from typing import Any

from app.core.logging import get_logger
from app.core.singleflight import SingleFlight
from app.http import get_http_client
from app.retrieval.ext_data._base import DEFAULT_TIMEOUT_S, TtlCache
from app.retrieval.ext_data.crypto import _key_tag

logger = get_logger(__name__)

_IPWHO_URL = "https://ipwho.is/{ip}"
_IPINFO_URL = "https://ipinfo.io/{ip}/json"

ipgeo_cache = TtlCache(ttl_s=3600.0, max_entries=128)
ipgeo_flight = SingleFlight()


def sanitize_ip(raw: str) -> str | None:
    ip = raw.strip()
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return None
    return str(parsed)


async def fetch_ipgeo(
    ip: str,
    *,
    ipinfo_token: str = "",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    client: Any = None,
) -> dict[str, Any] | None:
    clean = sanitize_ip(ip)
    if clean is None:
        return None
    cache_key = f"ipgeo:{clean}:{_key_tag(ipinfo_token)}"
    cached = ipgeo_cache.get(cache_key)
    if cached is not None:
        return dict(cached, cached=True)

    async def _load() -> dict[str, Any] | None:
        ipwho = await _fetch_ipwho(clean, timeout_s=timeout_s, client=client)
        ipinfo = None
        if ipinfo_token:
            ipinfo = await _fetch_ipinfo(
                clean, token=ipinfo_token, timeout_s=timeout_s, client=client
            )
        if ipwho is None and ipinfo is None:
            return None
        sources: list[str] = []
        if ipwho is not None:
            sources.append("ipwho.is")
        if ipinfo is not None:
            sources.append("IPinfo")
        return {
            "ip": clean,
            "ipwho": ipwho,
            "ipinfo": ipinfo,
            "sources_used": sources,
        }

    result = await ipgeo_flight.run(cache_key, _load)
    if result is None:
        return None
    ipgeo_cache.put(cache_key, result)
    return dict(result, cached=False)


async def _fetch_ipwho(ip: str, *, timeout_s: float, client: Any = None) -> dict[str, Any] | None:
    try:
        http_cli = client or get_http_client("search")
        resp = await http_cli.get(_IPWHO_URL.format(ip=ip), timeout=timeout_s)
        if resp.status_code != 200:
            logger.warning("ext_ipwho_failed", status=resp.status_code)
            return None
        data = resp.json()
    except Exception as exc:
        logger.warning("ext_ipwho_error", error=type(exc).__name__)
        return None
    if not isinstance(data, dict) or data.get("success") is not True:
        return None
    connection = data.get("connection") or {}
    flags = data.get("flag") or {}
    return {
        "country": str(data.get("country") or "")[:80],
        "country_code": str(data.get("country_code") or "")[:8],
        "region": str(data.get("region") or "")[:80],
        "city": str(data.get("city") or "")[:80],
        "latitude": _num(data.get("latitude")),
        "longitude": _num(data.get("longitude")),
        "timezone": str((data.get("timezone") or {}).get("id") or "")[:64]
        if isinstance(data.get("timezone"), dict)
        else None,
        "asn": str(connection.get("asn") or "")[:16],
        "org": str(connection.get("org") or "")[:120],
        "isp": str(data.get("connection", {}).get("isp") or "")[:120],
        "flag_emoji": str(flags.get("emoji") or "")[:8] or None,
        "source_name": "ipwho.is",
        "source_url": "https://ipwho.is",
    }


async def _fetch_ipinfo(
    ip: str,
    *,
    token: str,
    timeout_s: float,
    client: Any = None,
) -> dict[str, Any] | None:
    try:
        http_cli = client or get_http_client("search")
        resp = await http_cli.get(
            _IPINFO_URL.format(ip=ip),
            params={"token": token},
            timeout=timeout_s,
        )
        if resp.status_code != 200:
            logger.warning("ext_ipinfo_failed", status=resp.status_code)
            return None
        data = resp.json()
    except Exception as exc:
        logger.warning("ext_ipinfo_error", error=type(exc).__name__)
        return None
    if not isinstance(data, dict) or "error" in data:
        return None
    return {
        "city": str(data.get("city") or "")[:80],
        "region": str(data.get("region") or "")[:80],
        "country": str(data.get("country") or "")[:8],
        "loc": str(data.get("loc") or "")[:40],
        "org": str(data.get("org") or "")[:120],
        "timezone": str(data.get("timezone") or "")[:64],
        "source_name": "IPinfo",
        "source_url": "https://ipinfo.io",
    }


def _num(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return round(value, 4)
