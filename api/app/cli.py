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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "search":
            return _cmd_search(args)
        if args.command == "answer":
            return _cmd_answer(args)
    except httpx.HTTPStatusError as exc:
        _print_error(exc)
        return 1
    except httpx.HTTPError as exc:
        print(f"error: cannot reach API at {args.api_url} ({type(exc).__name__})", file=sys.stderr)
        return 1
    parser.error(f"unknown command: {args.command}")
    return 2


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
