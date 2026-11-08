"""MetaSearch thin Python client (WP-15) — one file, sync, only requests.

search() returns None on API error, [] only when complete=true with real empty results;
a list of dicts otherwise.
"""

from __future__ import annotations

import time
from email.utils import parsedate_to_datetime
from typing import Any

import requests


class MetaSearchError(Exception):
    def __init__(self, code: str, message: str, status_code: int, request_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.request_id = request_id


def _parse_retry_after(header_val: str | None) -> float:
    if not header_val:
        return 1.0
    try:
        return max(0.0, float(header_val))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(header_val)
        return max(0.0, dt.timestamp() - time.time())
    except Exception:
        return 1.0


last_request_id: str | None = None


def get_last_request_id() -> str | None:
    return last_request_id


def search(
    base_url: str,
    query: str,
    *,
    api_key: str | None = None,
    k: int = 10,
    lang: str | None = None,
    timeout: tuple[float, float] = (2.0, 30.0),
) -> list[dict[str, Any]] | None:
    """Search the web. Returns None on API error, [] only when complete=true with 0 results."""
    global last_request_id
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

        req_id = resp.headers.get("x-request-id") or resp.headers.get("X-Request-Id")
        if req_id:
            last_request_id = req_id

        body: Any = None
        try:
            body = resp.json()
            if isinstance(body, dict):
                if "query_id" in body and body["query_id"]:
                    last_request_id = str(body["query_id"])
                err = body.get("error")
                if isinstance(err, dict) and err.get("request_id"):
                    last_request_id = str(err["request_id"])
        except Exception:
            pass

        if resp.status_code == 200:
            data = body if isinstance(body, dict) else {}
            is_complete = bool(data.get("complete", True))
            results = data.get("results")
            if results:
                return results
            if is_complete:
                return []
            return None

        if resp.status_code in (401, 500):
            return None

        if resp.status_code in (429, 502, 503):
            if attempt == 1:
                return None
            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
            time.sleep(min(retry_after, 5.0))
            continue

        # Other errors: parse and raise
        err = body.get("error", {}) if isinstance(body, dict) else {}
        err_req_id = err.get("request_id") if isinstance(err, dict) else None
        err_req_id = err_req_id or last_request_id
        last_request_id = err_req_id
        err_code = err.get("code", "internal") if isinstance(err, dict) else "internal"
        err_msg = err.get("message", resp.text) if isinstance(err, dict) else resp.text
        raise MetaSearchError(err_code, err_msg, resp.status_code, err_req_id)
    return None
