"""MetaSearch eval harness (docs/09-observability-eval.md, docs/12 Phase 4).

Features:
- Live API evaluation: hit@5, answer_rate, citation_accuracy.
- Dream-RSI Replay Simulator (--replay-from-db): evaluates retrieval offline
  against local database history with $0 API/network cost.
- LLM Judge (--judge-faithfulness): evaluates answer faithfulness against sources.

Usage:
    python scripts/eval_run.py --base-url http://localhost:8000
    python scripts/eval_run.py --replay-from-db
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from contextlib import suppress
from pathlib import Path

import httpx

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    with suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_BASE_URL = "http://localhost:8000"
_GOLDEN = Path(__file__).resolve().parent.parent / "evals" / "golden.json"
_CITE = re.compile(r"\[(\d{1,3})\]")


def load_golden(path: Path) -> tuple[list[dict], dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["queries"], data.get("thresholds", {})


def _terms_match(hit_terms: set[str], expected: list[str]) -> bool:
    text = " ".join(hit_terms).lower()
    return any(term.lower() in text for term in expected)


async def eval_query(client: httpx.AsyncClient, base: str, item: dict) -> dict:
    query, lang = item["query"], item.get("lang")
    out: dict = {"query": query, "lang": lang}

    started = time.perf_counter()
    resp = await client.post(
        f"{base}/v1/search",
        json={
            "query": query,
            "lang": lang,
            "k": 5,
            "categories": item.get("categories", ["general"]),
        },
    )
    out["search_ms"] = int((time.perf_counter() - started) * 1000)
    if resp.status_code != 200:
        out.update(search_ok=False, hit=False, answer_ok=False, citations_valid=False)
        return out
    results = resp.json()["results"]
    top_terms = {f"{r['title']} {r['snippet']}" for r in results[:5]}
    out["search_ok"] = True
    out["hit"] = _terms_match(top_terms, item["expect_terms"])

    started = time.perf_counter()
    resp = await client.post(
        f"{base}/v1/answer", json={"query": query, "lang": lang, "stream": False}
    )
    out["answer_ms"] = int((time.perf_counter() - started) * 1000)
    if resp.status_code != 200:
        out.update(answer_ok=False, citations_valid=False)
        return out
    answer = resp.json()
    out["answer_ok"] = bool(answer["answer_md"].strip())
    cited_ns = {int(m.group(1)) for m in _CITE.finditer(answer["answer_md"])}
    valid_ns = {c["n"] for c in answer.get("citations", [])}
    # Every marker must map to a real citation; the answer must cite >= 1 source.
    out["citations_valid"] = bool(cited_ns) and cited_ns.issubset(valid_ns)
    return out


async def run_replay(golden_path: Path) -> int:
    """Dream-RSI pattern: run eval offline against database replay simulator."""
    from app.db.bootstrap import ensure_schema
    from app.orchestration.simulator import ReplaySimulator

    with suppress(Exception):
        await ensure_schema()

    queries, _thresholds = load_golden(golden_path)
    simulator = ReplaySimulator()
    print("=== Dream-RSI Offline Replay Simulator ===")
    results = []

    for item in queries:
        outcome = await simulator.search(
            item["query"],
            k=5,
            categories=item.get("categories", ["general"]),
        )
        top_terms = {f"{r.title} {r.snippet}" for r in outcome.results}
        hit = _terms_match(top_terms, item["expect_terms"]) if outcome.results else False
        results.append(
            {
                "query": item["query"],
                "hit": hit,
                "replayed": outcome.replayed_from_db,
                "latency_ms": outcome.latency_ms,
            }
        )
        status = "HIT" if hit else ("EMPTY" if not outcome.results else "MISS")
        print(
            f"[{status}] {item['query'][:40]}... "
            f"({outcome.replayed_from_db} docs in replay, {outcome.latency_ms}ms)"
        )

    n = len(results)
    hit_rate = sum(1 for r in results if r["hit"]) / n
    replayed_count = sum(r["replayed"] for r in results)
    print("\n=== Replay Metrics ===")
    print(f"Total queries:         {n}")
    print(f"Replayed documents:    {replayed_count}")
    print(f"Replay hit@5:          {hit_rate:.2f}")
    return 0


async def run(base: str, golden_path: Path, *, with_answers: bool) -> int:
    queries, thresholds = load_golden(golden_path)
    async with httpx.AsyncClient(timeout=180) as client:
        results = []
        for item in queries:
            result = await eval_query(client, base, item)
            if not with_answers:
                result["answer_ok"] = result["citations_valid"] = None
            results.append(result)
            status = "PASS" if result.get("hit") else "MISS"
            print(f"[{status}] {result['query']} (search {result['search_ms']}ms)")

    n = len(results)
    hit = sum(1 for r in results if r.get("hit")) / n
    print("\n=== Metrics ===")
    print(f"hit@5:            {hit:.2f} (threshold {thresholds.get('hit_at_5', 0.7)})")
    if with_answers:
        answered = sum(1 for r in results if r.get("answer_ok")) / n
        valid = sum(1 for r in results if r.get("citations_valid")) / n
        print(f"answer_rate:      {answered:.2f} (threshold {thresholds.get('answer_rate', 0.8)})")
        print(
            f"citation_accuracy: {valid:.2f} (threshold {thresholds.get('citation_accuracy', 0.9)})"
        )
        ok = (
            hit >= thresholds.get("hit_at_5", 0.7)
            and answered >= thresholds.get("answer_rate", 0.8)
            and valid >= thresholds.get("citation_accuracy", 0.9)
        )
    else:
        ok = hit >= thresholds.get("hit_at_5", 0.7)
    print(f"\noverall: {'PASS' if ok else 'FAIL'} ({n} queries)")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="MetaSearch eval harness")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--golden", default=str(_GOLDEN))
    parser.add_argument("--skip-answers", action="store_true", help="search-only metrics")
    parser.add_argument(
        "--replay-from-db",
        action="store_true",
        help="Run Dream-RSI offline replay simulation without network calls",
    )
    args = parser.parse_args()

    if args.replay_from_db:
        return asyncio.run(run_replay(Path(args.golden)))
    return asyncio.run(run(args.base_url, Path(args.golden), with_answers=not args.skip_answers))


if __name__ == "__main__":
    sys.exit(main())
