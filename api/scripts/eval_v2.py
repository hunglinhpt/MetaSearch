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

from app.eval_judge import llm_judge
from app.eval_metrics import (
    aggregate,
    aggregate_by_slice,
    citation_precision_rate,
    cited_urls_from_answer,
    claim_support_rate,
    context_precision,
    context_recall,
    hit_at_k,
    latency_percentiles,
    mrr,
    ndcg_at_k,
    numeric_fidelity_rate,
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
    with_judge: bool,
    gateway: object | None,
    concurrency_sem: asyncio.Semaphore,
) -> dict:
    query = q["query"]
    relevant = q.get("relevant") or []
    cats = q.get("categories") or ["general"]
    record: dict = {
        "query": query,
        "lang": q.get("lang"),
        "slice": f"{q.get('lang') or 'auto'}:{cats[0]}",
    }
    snippet_by_url: dict[str, str] = {}
    t0 = time.perf_counter()
    async with concurrency_sem:
        try:
            r = await client.post(
                f"{base_url}/v1/search",
                json={
                    "query": query,
                    "lang": q.get("lang"),
                    "k": 10,
                    "categories": cats,
                },
                timeout=60.0,
            )
            r.raise_for_status()
            items = r.json().get("results", [])
            urls = [item.get("url", "") for item in items]
            for item in items:
                u = item.get("url") or ""
                if u:
                    snippet_by_url[u] = " ".join(
                        p for p in (item.get("title") or "", item.get("snippet") or "") if p
                    ).strip()
        except Exception as exc:
            record["error"] = f"search: {type(exc).__name__}"
            urls = []
    record["search_latency_ms"] = int((time.perf_counter() - t0) * 1000)
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
                        "categories": cats,
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
        snippet_by_n: dict[int, str] = {}
        for item in payload.get("sources") or []:
            su = item.get("url") or ""
            if su and su not in snippet_by_url:
                snippet_by_url[su] = " ".join(
                    p for p in (item.get("title") or "", item.get("snippet") or "") if p
                ).strip()
        for c in citations:
            n = int(c.get("n") or 0)
            cu = c.get("url") or ""
            if cu in snippet_by_url:
                snippet_by_n[n] = snippet_by_url[cu]
            elif c.get("title"):
                snippet_by_n[n] = str(c["title"])
        record["claim_support"] = claim_support_rate(answer_md, snippet_by_n) if answer_md else 0.0
        record["numeric_fidelity"] = (
            numeric_fidelity_rate(answer_md, snippet_by_n) if answer_md else 0.0
        )
        record["citation_precision"] = (
            citation_precision_rate(answer_md, {int(c.get("n") or 0) for c in citations})
            if answer_md
            else 0.0
        )
        record["unsupported_markers"] = payload.get("unsupported_markers") or []
        record["follow_ups"] = len(payload.get("follow_ups") or [])
        if with_judge and gateway is not None and answer_md:
            record["judge"] = await llm_judge(
                gateway,
                answer_md,
                query,
                [snippet_by_n[k] for k in sorted(snippet_by_n) if snippet_by_n[k]],
            )
    return record


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--golden", type=Path, default=_GOLDEN)
    parser.add_argument("--report-dir", type=Path, default=_DEFAULT_REPORT_DIR)
    parser.add_argument("--answers", action="store_true", help="also evaluate /v1/answer")
    parser.add_argument("--judge", action="store_true", help="also run RAGAS LLM-as-judge")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0, help="only first N queries (debug)")
    args = parser.parse_args()

    queries, thresholds = load_golden(args.golden)
    if args.limit:
        queries = queries[: args.limit]

    gateway = None
    if args.judge:
        try:
            from app.api import deps

            gateway = deps.get_gateway()
        except Exception:
            gateway = None

    sem = asyncio.Semaphore(max(1, args.concurrency))
    started = time.perf_counter()
    async with httpx.AsyncClient() as client:
        records = await asyncio.gather(
            *(
                eval_query(
                    client,
                    args.base_url,
                    q,
                    with_answers=args.answers,
                    with_judge=args.judge,
                    gateway=gateway,
                    concurrency_sem=sem,
                )
                for q in queries
            )
        )
    rec_list = list(records)
    summary = aggregate(rec_list)
    lat_stats = latency_percentiles(
        [float(r["search_latency_ms"]) for r in rec_list if r.get("search_latency_ms") is not None]
    )
    by_slice = aggregate_by_slice(rec_list, key="slice")
    elapsed = time.perf_counter() - started

    report = {
        "ts": datetime.now(UTC).isoformat(),
        "base_url": args.base_url,
        "golden": str(args.golden),
        "with_answers": args.answers,
        "with_judge": args.judge,
        "elapsed_s": round(elapsed, 1),
        "summary": summary,
        "latency_percentiles": lat_stats,
        "by_slice": by_slice,
        "thresholds": thresholds,
        "passed": all(summary.get(k, 0.0) >= v for k, v in thresholds.items() if k in summary),
        "records": rec_list,
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
