"""Shared keyless-JSON GET helper for the v2.4.0 global adapters (docs/19 W1).

One place maps httpx failures to ``AdapterError`` with the right ``Outcome`` kind
so the health breaker classifies them correctly — instead of every adapter
re-implementing the same try/except ladder.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.http import get_http_client
from app.retrieval.adapters.base import AdapterError
from app.retrieval.health import Outcome

DEFAULT_UA = (
    "MetaSearch/2.4.0 (https://github.com/hunglinhpt/MetaSearch; contact: admin@metasearch.local)"
)


async def fetch_json(
    adapter_id: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: float = 4.0,
    client: httpx.AsyncClient | None = None,
    max_bytes: int = 5 * 1024 * 1024,
) -> Any:
    """GET ``url`` and return parsed JSON; raises AdapterError on any failure."""
    hdrs = {"User-Agent": DEFAULT_UA, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    http_client = client or get_http_client("search")
    try:
        # follow_redirects stays at the pool default (False, AGENTS §4): none
        # of the fixed API hosts need it, and a redirect would send our
        # headers to an arbitrary third host with no per-hop re-check.
        resp = await http_client.get(url, params=params, headers=hdrs, timeout=timeout_s)
    except httpx.PoolTimeout as exc:
        raise AdapterError(f"{adapter_id} pool timeout", kind=Outcome.LOCAL_OVERLOAD) from exc
    except httpx.TimeoutException as exc:
        raise AdapterError(
            f"{adapter_id} timeout after {timeout_s}s", kind=Outcome.TIMEOUT
        ) from exc
    except httpx.HTTPError as exc:
        raise AdapterError(
            f"{adapter_id} transport error: {type(exc).__name__}", kind=Outcome.UPSTREAM_ERROR
        ) from exc
    if resp.status_code == 429:
        raise AdapterError(f"{adapter_id} rate limited", kind=Outcome.RATE_LIMITED)
    if resp.status_code == 403:
        raise AdapterError(f"{adapter_id} access forbidden", kind=Outcome.BLOCKED, retryable=False)
    if resp.status_code != 200:
        raise AdapterError(
            f"{adapter_id} HTTP {resp.status_code}",
            kind=Outcome.UPSTREAM_ERROR,
            retryable=False,
        )
    # NOTE: the byte cap below runs after the body is buffered; it bounds
    # what we PARSE. A streaming hard-cap is tracked as follow-up polish.
    if len(resp.content or b"") > max_bytes:
        raise AdapterError(
            f"{adapter_id} payload too large", kind=Outcome.UPSTREAM_ERROR, retryable=False
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise AdapterError(f"{adapter_id} returned non-JSON body") from exc
