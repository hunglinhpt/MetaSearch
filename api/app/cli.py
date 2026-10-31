"""``metasearch`` console script.

CLI calls the REST API (default http://localhost:8000) so it stays thin and
works against any deployment; ``--api-url`` points it elsewhere.
    metasearch search "query" [--lang vi] [-k 10] [--json]
    metasearch answer "query" [--lang vi] [--stream] [--model gpt-4o-mini]
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

from app import __version__

DEFAULT_API_URL = "http://localhost:8000"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="metasearch", description="MetaSearch CLI")
    parser.add_argument("--version", action="version", version=f"metasearch {__version__}")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="MetaSearch API base URL")
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="Raw search: ranked results, no LLM")
    p_search.add_argument("query")
    p_search.add_argument("--lang", default=None, help="2-letter language, e.g. vi")
    p_search.add_argument("-k", type=int, default=10, help="max results")
    p_search.add_argument("--json", action="store_true", help="print raw JSON")

    p_answer = sub.add_parser("answer", help="Quick answer with citations (LLM)")
    p_answer.add_argument("query")
    p_answer.add_argument("--lang", default=None, help="2-letter language, e.g. vi")
    p_answer.add_argument("--model", default=None, help="LLM model override")
    p_answer.add_argument("--stream", action="store_true", help="stream tokens as they arrive")

    p_keys = sub.add_parser("keys", help="Manage API keys (requires DB access)")
    keys_sub = p_keys.add_subparsers(dest="keys_command", required=True)
    p_create = keys_sub.add_parser("create", help="Create an API key (printed once)")
    p_create.add_argument("name")
    p_create.add_argument("--role", default="user", choices=["user", "admin"])
    p_create.add_argument("--rate-limit", type=int, default=60, help="requests per minute")
    keys_sub.add_parser("list", help="List existing API keys")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "search":
            return _cmd_search(args)
        if args.command == "answer":
            return _cmd_answer(args)
        if args.command == "keys":
            return _cmd_keys(args)
    except httpx.HTTPStatusError as exc:
        _print_error(exc)
        return 1
    except httpx.HTTPError as exc:
        print(f"error: cannot reach API at {args.api_url} ({type(exc).__name__})", file=sys.stderr)
        return 1
    parser.error(f"unknown command: {args.command}")
    return 2


def _cmd_keys(args: argparse.Namespace) -> int:
    """Create/list API keys directly against the configured database."""
    import asyncio

    from sqlalchemy import select

    from app.core.auth import create_key
    from app.db.models import ApiKey
    from app.db.session import dispose_engine, get_sessionmaker

    async def _run() -> int:
        if args.keys_command == "create":
            plaintext, prefix = await create_key(
                args.name, role=args.role, rate_limit_rpm=args.rate_limit
            )
            print(f"key:     {plaintext}")
            print(f"prefix:  {prefix}")
            print(f"role:    {args.role}  rate_limit: {args.rate_limit}/min")
            print("store it now - the plaintext is not shown again.")
            return 0

        async with get_sessionmaker()() as session:
            rows = (
                (await session.execute(select(ApiKey).order_by(ApiKey.created_at.desc())))
                .scalars()
                .all()
            )
        print(f"{'prefix':<16} {'role':<6} {'rpm':<5} {'enabled':<8} name")
        for row in rows:
            print(
                f"{(row.key_prefix or '-'):<16} {row.role or 'user':<6} "
                f"{row.rate_limit_rpm or 60:<5} {bool(row.enabled)!s:<8} {row.name or '-'}"
            )
        return 0

    async def _run_and_close() -> int:
        try:
            return await _run()
        except Exception as exc:
            print(f"error: database unreachable ({type(exc).__name__}: {exc})", file=sys.stderr)
            return 1
        finally:
            await dispose_engine()

    return asyncio.run(_run_and_close())


def _post(api_url: str, path: str, payload: dict) -> httpx.Response:
    resp = httpx.post(f"{api_url.rstrip('/')}{path}", json=payload, timeout=120)
    resp.raise_for_status()
    return resp


def _cmd_search(args: argparse.Namespace) -> int:
    payload: dict = {"query": args.query, "k": args.k}
    if args.lang:
        payload["lang"] = args.lang
    resp = _post(args.api_url, "/v1/search", payload)
    data = resp.json()
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    for r in data["results"]:
        published = f"  ({r['published_at'][:10]})" if r.get("published_at") else ""
        print(f"{r['rank']:>2}. {r['title']}{published}")
        print(f"    {r['url']}  [{r['score']:.3f}] by {', '.join(r['found_by'])}")
        if r.get("snippet"):
            print(f"    {r['snippet'][:160]}")
    used = ", ".join(data["engines_used"]) or "-"
    failed = f"  failed: {', '.join(data['engines_failed'])}" if data["engines_failed"] else ""
    print(f"\n{len(data['results'])} results in {data['latency_ms']} ms (engines: {used}{failed})")
    return 0


def _cmd_answer(args: argparse.Namespace) -> int:
    payload: dict = {"query": args.query}
    if args.lang:
        payload["lang"] = args.lang
    if args.model:
        payload["model"] = args.model
    if args.stream:
        payload["stream"] = True
        citations: list[dict] = []
        with (
            httpx.Client(timeout=180) as client,
            client.stream("POST", f"{args.api_url.rstrip('/')}/v1/answer", json=payload) as resp,
        ):
            resp.raise_for_status()
            event = ""
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    data = json.loads(line.split(":", 1)[1])
                    if event == "token":
                        print(data["t"], end="", flush=True)
                    elif event == "citations":
                        citations = data["citations"]
                    elif event == "error":
                        print(f"\nerror: {data['message']}", file=sys.stderr)
                        return 1
        print()
        print_citations(citations)
        return 0

    data = _post(args.api_url, "/v1/answer", payload).json()
    print(data["answer_md"])
    print()
    print_citations(data["citations"])
    print(f"\nmodel: {data['model']}  latency: {data['latency_ms']} ms")
    return 0


def print_citations(citations: list[dict]) -> None:
    if not citations:
        return
    print("\nSources:")
    for c in citations:
        print(f"  [{c['n']}] {c.get('title') or c['url']}")
        print(f"      {c['url']}")


def _print_error(exc: httpx.HTTPStatusError) -> None:
    try:
        detail = exc.response.json()["error"]["message"]
    except Exception:
        detail = exc.response.text[:200]
    print(f"error: HTTP {exc.response.status_code}: {detail}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
