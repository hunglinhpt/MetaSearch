"""MetaSearch eval harness v2 (docs/20 P0-8, v2.5.0).

Extends eval_run.py with retrieval metrics (hit@k, recall@k, MRR, nDCG@10)
and RAGAS-style context precision/recall + claim support, over the v2 golden
set (evals/golden_v2.json — queries carry `relevant` URL substrings).

Usage:
    python scripts/eval_v2.py --base-url http://localhost:8000
    python scripts/eval_v2.py --base-url http://localhost:8000 --answers   # also /v1/answer metrics
    python scripts/eval_v2.py --base-url http://localhost:8000 --report-dir evals/reports

Writes a timestamped JSON report and prints a delta vs the previous report
when one exists, so a version bump can never silently regress retrieval.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.eval_metrics import (
    aggregate,
    cited_urls_from_answer,
    claim_support_rate,
    context_precision,
    context_recall,
    hit_at_k,
    mrr,
    ndcg_at_k,
    recall_at_k,
)

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    with suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_GOLDEN = Path(__file__).resolve().parent.parent / "evals" / "golden_v2.json"
_DEFAULT_REPORT_DIR = Path(__file__).resolve().parent.parent / "evals" / "reports"


def load_golden(path: Path) -> tuple[list[dict], dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["queries"], data.get("thresholds", {})


async def eval_query(
    client: httpx.AsyncClient,
    base_url: str,
    q: dict,
    *,
    with_answers: bool,
    concurrency_sem: asyncio.Semaphore,
) -> dict:
    query = q["query"]
    relevant = q.get("relevant") or []
    record: dict = {"query": query, "lang": q.get("lang")}
    async with concurrency_sem:
        try:
            r = await client.post(
                f"{base_url}/v1/search",
                json={
                    "query": query,
                    "lang": q.get("lang"),
                    "k": 10,
                    "categories": q.get("categories") or ["general"],
                },
                timeout=60.0,
            )
            r.raise_for_status()
            urls = [item.get("url", "") for item in r.json().get("results", [])]
        except Exception as exc:
            record["error"] = f"search: {type(exc).__name__}"
            urls = []
    record["hit_at_5"] = hit_at_k(urls, relevant, 5)
    record["hit_at_10"] = hit_at_k(urls, relevant, 10)
    record["recall_at_10"] = recall_at_k(urls, relevant, 10)
    record["mrr"] = mrr(urls, relevant)
    record["ndcg_at_10"] = ndcg_at_k(urls, relevant, 10)

    if with_answers and urls:
        async with concurrency_sem:
            try:
                r = await client.post(
                    f"{base_url}/v1/answer",
                    json={
                        "query": query,
                        "lang": q.get("lang"),
                        "k": 10,
                        "categories": q.get("categories") or ["general"],
                    },
                    timeout=180.0,
                )
                r.raise_for_status()
                payload = r.json()
            except Exception as exc:
                record["error"] = f"answer: {type(exc).__name__}"
                payload = {}
        answer_md = payload.get("answer_md") or ""
        citations = payload.get("citations") or []
        cited = cited_urls_from_answer(answer_md, citations)
        record["context_precision"] = context_precision(cited, relevant)
        record["context_recall"] = context_recall(cited, relevant)
        # claim support: fetch chunk contents is not exposed — approximate with
        # snippets of the cited search results (documented limitation).
        snippet_by_n: dict[int, str] = {}
        for c in citations:
            n = int(c.get("n") or 0)
            for item in payload.get("sources") or []:
                if item.get("url") == c.get("url"):
                    snippet_by_n[n] = item.get("title", "")
        record["claim_support"] = claim_support_rate(answer_md, snippet_by_n) if answer_md else 0.0
        record["unsupported_markers"] = payload.get("unsupported_markers") or []
        record["follow_ups"] = len(payload.get("follow_ups") or [])
    return record


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--golden", type=Path, default=_GOLDEN)
    parser.add_argument("--report-dir", type=Path, default=_DEFAULT_REPORT_DIR)
    parser.add_argument("--answers", action="store_true", help="also evaluate /v1/answer")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0, help="only first N queries (debug)")
    args = parser.parse_args()

    queries, thresholds = load_golden(args.golden)
    if args.limit:
        queries = queries[: args.limit]

    sem = asyncio.Semaphore(max(1, args.concurrency))
    started = time.perf_counter()
    async with httpx.AsyncClient() as client:
        records = await asyncio.gather(
            *(
                eval_query(client, args.base_url, q, with_answers=args.answers, concurrency_sem=sem)
                for q in queries
            )
        )
    summary = aggregate(list(records))
    elapsed = time.perf_counter() - started

    report = {
        "ts": datetime.now(UTC).isoformat(),
        "base_url": args.base_url,
        "golden": str(args.golden),
        "with_answers": args.answers,
        "elapsed_s": round(elapsed, 1),
        "summary": summary,
        "thresholds": thresholds,
        "passed": all(summary.get(k, 0.0) >= v for k, v in thresholds.items() if k in summary),
        "records": list(records),
    }

    args.report_dir.mkdir(parents=True, exist_ok=True)
    out = args.report_dir / f"eval_v2_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # delta vs previous report
    previous = sorted(args.report_dir.glob("eval_v2_*.json"))
    delta = None
    if len(previous) >= 2 and previous[-2] != out:
        try:
            prev = json.loads(previous[-2].read_text(encoding="utf-8"))
            delta = {
                k: round(summary.get(k, 0.0) - prev.get("summary", {}).get(k, 0.0), 4)
                for k in summary
                if k != "n_queries"
            }
        except Exception:
            delta = None

    print(
        json.dumps(
            {
                "report": str(out),
                "summary": summary,
                "passed": report["passed"],
                "delta_vs_prev": delta,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
