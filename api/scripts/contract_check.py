"""Check a running MetaSearch against the /v1 contract, from the outside.

Usage:
    python scripts/contract_check.py --base-url http://HOST:PORT [--key API_KEY]

Needs only httpx. Does not import the server code, so it runs from any machine that can
reach the API. There is deliberately no default base URL: point it at the instance you
mean to check. Exit code 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from typing import Any

import httpx

MARKS = ("source_url", "data_kind", "published_at", "as_of", "age_s")
CHUNK_MARKS = ("source_url", "data_kind", "fetched_at", "published_at", "as_of", "age_s")


class Checker:
    def __init__(self, base_url: str, key: str | None, timeout_s: float) -> None:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_s
        )
        self.failed = 0
        self.passed = 0

    def check(self, ok: bool, name: str, detail: str = "") -> bool:
        if ok:
            self.passed += 1
        else:
            self.failed += 1
        print(
            f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail and not ok else "")
        )
        return ok

    def post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        return self.client.post(path, json=body)

    def error_model(self, resp: httpx.Response) -> bool:
        try:
            err = resp.json()["error"]
        except Exception:
            return False
        return isinstance(err, dict) and bool(err.get("code")) and "message" in err


def run(c: Checker) -> None:
    tag = uuid.uuid4().hex[:8]

    r = c.client.get("/healthz")
    c.check(r.status_code == 200, "GET /healthz is 200", f"status={r.status_code}")

    # Error model: every 4xx/5xx carries {"error": {"code", "message"}}.
    r = c.post("/v1/search", {})
    c.check(
        r.status_code == 400 and c.error_model(r),
        "invalid request is 400 with the error model",
        f"status={r.status_code} body={r.text[:160]}",
    )

    # Search
    r = c.post("/v1/search", {"query": f"contract check {tag}", "k": 5})
    results: list[dict[str, Any]] = []
    if r.status_code == 200:
        body = r.json()
        results = body.get("results") or []
        c.check(isinstance(body.get("complete"), bool), "search has a complete flag")
        missing = sorted({k for x in results for k in MARKS if k not in x})
        c.check(
            not missing,
            "search results carry provenance marks (null when unknown)",
            f"missing={missing}",
        )
    else:
        c.check(
            r.status_code in (503, 504) and c.error_model(r),
            "search failure uses 503/504 and the error model",
            f"status={r.status_code}",
        )

    if results:
        r = c.post(
            "/v1/search",
            {"query": f"contract check {tag}", "k": 5, "include_domains": ["nowhere.invalid"]},
        )
        body = r.json() if r.status_code == 200 else {}
        c.check(
            r.status_code == 200 and body.get("results") == [] and body.get("complete") is False,
            "a filter that removes every result says complete=false",
            f"status={r.status_code} complete={body.get('complete')}",
        )
        domain = results[0].get("domain") or ""
        if domain:
            r = c.post(
                "/v1/search",
                {"query": f"contract check {tag}", "k": 5, "exclude_domains": [domain]},
            )
            got = (
                [x.get("domain") for x in (r.json().get("results") or [])]
                if r.status_code == 200
                else None
            )
            c.check(
                got is not None and domain not in got,
                "exclude_domains drops that domain",
                f"domains={got}",
            )

    # Context
    r = c.post("/v1/context", {"query": f"contract context {tag}", "k": 3})
    if r.status_code == 200:
        body = r.json()
        chunks = body.get("chunks") or []
        c.check(
            "chunks" in body and "sources" in body, "context returns chunks and sources by default"
        )
        c.check(
            "context_md" not in body and "messages" not in body,
            "default context leaves out context_md and messages",
            f"keys={sorted(body)}",
        )
        missing = sorted({k for x in chunks for k in CHUNK_MARKS if k not in x})
        c.check(
            bool(chunks) and not missing,
            "context chunks carry provenance marks",
            f"chunks={len(chunks)} missing={missing}",
        )
        r2 = c.post(
            "/v1/context",
            {
                "query": f"contract context {tag}",
                "k": 3,
                "include": ["chunks", "context_md", "messages"],
            },
        )
        b2 = r2.json() if r2.status_code == 200 else {}
        c.check(
            "context_md" in b2 and "messages" in b2,
            "include brings context_md and messages back",
            f"status={r2.status_code}",
        )
    else:
        c.check(
            r.status_code in (503, 504) and c.error_model(r),
            "context failure uses 503/504 and the error model",
            f"status={r.status_code}",
        )

    # Citations
    r = c.post(
        "/v1/citations/validate",
        {
            "answer_md": "Claim [1]. Bad [9].",
            "chunks": [{"url": "https://a.example/x", "content": "text"}],
        },
    )
    body = r.json() if r.status_code == 200 else {}
    c.check(
        r.status_code == 200
        and body.get("marker_valid") is False
        and body.get("invalid_markers") == [9],
        "citations report marker_valid and the invalid markers",
        f"status={r.status_code} body={str(body)[:160]}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True, help="API root, e.g. http://127.0.0.1:8000")
    parser.add_argument("--key", default=None, help="API key sent as a Bearer token")
    parser.add_argument("--timeout", type=float, default=30.0, help="per request, seconds")
    args = parser.parse_args(argv)
    c = Checker(args.base_url, args.key, args.timeout)
    try:
        run(c)
    except httpx.HTTPError as exc:
        c.check(False, "API reachable", f"{type(exc).__name__}: {exc}")
    print(f"\n{c.passed} passed, {c.failed} failed")
    return 0 if c.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
