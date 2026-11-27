"""Live Deep Research eval (docs/36 R1b).

Submits each case of ``evals/research_live_golden.json`` to ``POST /v1/research``,
polls the job (bounded), and scores what came back:

- ``completed`` — job finished with a non-empty report;
- ``domain_hit`` — an expected domain is among the cited URLs;
- ``key_term_coverage`` — share of topic terms present in the report (accents kept);
- ``citation_coverage`` / ``unsupported_ratio`` / ``numeric_fidelity`` — the
  server's own ``verification`` block (a self-check, reported as such).

Run against a SEPARATE instance (never the Lite deployment on :8000 — it writes
research jobs into the integration DB):

    python scripts/eval_research_live.py --base-url http://127.0.0.1:8011 --limit 20
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
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.retrieval.scoring import fold_diacritics

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    with suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_GOLDEN = Path(__file__).resolve().parent.parent / "evals" / "research_live_golden.json"
_DEFAULT_REPORT_DIR = Path(__file__).resolve().parent.parent / "evals" / "reports"


def term_coverage(report: str, terms: list[str]) -> float:
    if not terms:
        return 1.0
    low = report.lower()
    folded = fold_diacritics(low)
    hits = 0
    for term in terms:
        t = term.lower()
        # accented terms must match with accents; unaccented ones may match folded text
        hay = folded if t == fold_diacritics(t) else low
        hits += 1 if t in hay else 0
    return hits / len(terms)


def domain_hit(citations: list[dict], expected: list[str]) -> float:
    hosts = {(urlsplit(str(c.get("url") or "")).hostname or "").lower() for c in citations}
    return 1.0 if any(h == d or h.endswith("." + d) for h in hosts for d in expected) else 0.0


def score_case(case: dict, job: dict, elapsed_s: float) -> dict:
    report = str(job.get("report_md") or "")
    citations = list(job.get("citations") or [])
    ver = job.get("verification") or {}
    count = int(ver.get("citation_count") or len(citations) or 0)
    unsupported = len(ver.get("unsupported_markers") or [])
    return {
        "id": case["id"],
        "status": job.get("status"),
        "error": job.get("error"),
        "elapsed_s": round(elapsed_s, 1),
        "completed": 1.0 if job.get("status") == "done" and report.strip() else 0.0,
        "domain_hit": domain_hit(citations, case.get("expected_domains") or []),
        "key_term_coverage": round(term_coverage(report, case.get("key_terms") or []), 4),
        "citation_coverage": float(ver.get("citation_coverage") or 0.0),
        "unsupported_ratio": round(unsupported / count, 4) if count else None,
        "numeric_fidelity": ver.get("numeric_fidelity"),
        "citations": len(citations),
        "report_chars": len(report),
    }


async def run_case(
    client: httpx.AsyncClient, base: str, case: dict, *, timeout_s: float, poll_s: float
) -> dict:
    started = time.perf_counter()
    try:
        r = await client.post(
            f"{base}/v1/research",
            json={"query": case["query"], "lang": case.get("lang"), "breadth": 2, "depth": 1},
            timeout=30,
        )
        r.raise_for_status()
        rid = r.json()["research_id"]
    except Exception as exc:
        return score_case(case, {"status": "submit_failed", "error": type(exc).__name__}, 0.0)
    job: dict = {}
    while time.perf_counter() - started < timeout_s:
        await asyncio.sleep(poll_s)
        try:
            job = (await client.get(f"{base}/v1/research/{rid}", timeout=30)).json()
        except Exception:
            continue
        if job.get("status") in ("done", "failed", "cancelled"):
            break
    else:
        job = {**job, "status": job.get("status") or "timeout", "error": "eval timeout"}
    return score_case(case, job, time.perf_counter() - started)


def summarize(rows: list[dict]) -> dict:
    def mean(key: str) -> float:
        vals = [float(r[key]) for r in rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 4) if vals else 0.0

    return {
        "n": len(rows),
        "completed_rate": mean("completed"),
        "domain_hit_rate": mean("domain_hit"),
        "key_term_coverage": mean("key_term_coverage"),
        "citation_coverage": mean("citation_coverage"),
        "unsupported_ratio": mean("unsupported_ratio"),
        "numeric_fidelity": mean("numeric_fidelity"),
        "median_elapsed_s": sorted(r["elapsed_s"] for r in rows)[len(rows) // 2] if rows else 0,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8011")
    parser.add_argument("--golden", type=Path, default=_GOLDEN)
    parser.add_argument("--report-dir", type=Path, default=_DEFAULT_REPORT_DIR)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout-s", type=float, default=600.0)
    parser.add_argument("--poll-s", type=float, default=5.0)
    args = parser.parse_args()

    data = json.loads(args.golden.read_text(encoding="utf-8"))
    cases = data["cases"][: args.limit] if args.limit else data["cases"]
    thresholds = data.get("thresholds", {})
    rows: list[dict] = []
    async with httpx.AsyncClient() as client:
        for case in cases:  # sequential: research is LLM-bound
            row = await run_case(
                client, args.base_url, case, timeout_s=args.timeout_s, poll_s=args.poll_s
            )
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    summary = summarize(rows)
    passed = all(summary.get(k, 0.0) >= v for k, v in thresholds.items())
    report = {
        "ts": datetime.now(UTC).isoformat(),
        "base_url": args.base_url,
        "summary": summary,
        "thresholds": thresholds,
        "passed": passed,
        "records": rows,
    }
    args.report_dir.mkdir(parents=True, exist_ok=True)
    out = args.report_dir / f"eval_research_live_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(out), "summary": summary, "passed": passed}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
