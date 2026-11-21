"""Shared plumbing for external data sources (docs/32 §1).

A tiny TTL cache (monotonic-clock, bounded) plus the shared HTTP constants
re-exported from the FA-sources base. Every public fetcher combines
cache + single-flight so free-tier quotas survive request bursts.
"""

from __future__ import annotations

import time
from typing import Any

from app.retrieval.fa_sources.base import BROWSER_HEADERS, MAX_RESPONSE_BYTES  # noqa: F401

DEFAULT_TIMEOUT_S = 8.0


class TtlCache:
    """Monotonic-clock TTL cache with an upper entry bound (oldest evicted)."""

    __slots__ = ("_data", "_max_entries", "_ttl_s")

    def __init__(self, ttl_s: float, max_entries: int = 64) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._ttl_s = ttl_s
        self._max_entries = max_entries

    def get(self, key: str) -> Any | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        stored_at, value = hit
        if time.monotonic() - stored_at > self._ttl_s:
            self._data.pop(key, None)
            return None
        return value

    def put(self, key: str, value: Any) -> None:
        self._data[key] = (time.monotonic(), value)
        while len(self._data) > self._max_entries:
            oldest = min(self._data, key=lambda k: self._data[k][0])
            self._data.pop(oldest, None)

    def clear(self) -> None:
        self._data.clear()


def ext_settings_keys(settings: Any) -> dict[str, str]:
    """Collect the optional registered-key env values for the docs/32 sources.

    Values may be empty strings — every fetcher treats an empty key as "the
    keyless path only" and callers report provenance honestly."""
    return {
        "coingecko_api_key": str(getattr(settings, "coingecko_api_key", "") or ""),
        "openweather_api_key": str(getattr(settings, "openweather_api_key", "") or ""),
        "newsapi_key": str(getattr(settings, "newsapi_key", "") or ""),
        "alpha_vantage_api_key": str(getattr(settings, "alpha_vantage_api_key", "") or ""),
        "twelvedata_api_key": str(getattr(settings, "twelvedata_api_key", "") or ""),
        "fmp_api_key": str(getattr(settings, "fmp_api_key", "") or ""),
        "ipinfo_token": str(getattr(settings, "ipinfo_token", "") or ""),
        "exchangerate_api_key": str(getattr(settings, "exchangerate_api_key", "") or ""),
    }


def json_get(payload: Any, *path: str) -> Any:
    """Walk a JSON tree defensively: returns None on any missing/non-dict hop."""
    node = payload
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node
