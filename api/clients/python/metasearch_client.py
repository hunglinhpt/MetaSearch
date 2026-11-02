"""MetaSearch thin Python client (WP-15) — one file, sync, only requests.

search() returns None when the API is unreachable; [] only when
complete=true with real results; a list of dicts otherwise.
"""

from __future__ import annotations

import time
from typing import Any

import requests


class MetaSearchError(Exception):
    def __init__(self, code: str, message: str, status_code: int, request_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.request_id = request_id


def search(
    base_url: str,
    query: str,
    *,
    api_key: str | None = None,
    k: int = 10,
    lang: str | None = None,
    timeout: tuple[float, float] = (2.0, 30.0),
) -> list[dict[str, Any]] | None:
    """Search the web. Returns None on API error, [] only when complete=true."""
    url = f"{base_url.rstrip('/')}/v1/search"
    payload: dict[str, Any] = {"query": query, "k": k}
    if lang:
        payload["lang"] = lang
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    for attempt in range(2):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException:
            if attempt == 1:
                return None
            time.sleep(1)
            continue

        if resp.status_code == 200:
            data = resp.json()
            if data.get("complete", True):
                return data.get("results", [])
            return []  # incomplete but valid — partial results
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", 1))
            time.sleep(min(retry_after, 5))
            continue
        if resp.status_code in (502, 503):
            if attempt == 1:
                return None
            time.sleep(1)
            continue
        # Other errors: parse and raise
        try:
            err = resp.json()["error"]
            raise MetaSearchError(
                err.get("code", "internal"),
                err.get("message", resp.text),
                resp.status_code,
                err.get("request_id"),
            )
        except (KeyError, ValueError):
            raise MetaSearchError("internal", f"HTTP {resp.status_code}", resp.status_code)
    return None
