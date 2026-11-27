"""Run the claim-verification gold set against a running API (docs/36 T1).

    python scripts/eval_verify.py --base-url http://127.0.0.1:8011 --report-dir evals/reports/verify

Writes a timestamped JSON report (per-claim verdicts + summary from
``app.eval_verify.score``). Exit 0 when the safety target holds
(false_support_rate <= 0.05 and no call errors), 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.eval_verify import score

_GOLDEN = Path(__file__).resolve().parent.parent / "evals" / "verify_golden.json"


@contextlib.asynccontextmanager
async def _client(base_url: str, in_process: bool):
    """HTTP client for a running API, or the app itself in-process (no port, no
    server process to stop afterwards)."""
    if not in_process:
        async with httpx.AsyncClient(base_url=base_url, timeout=90.0) as c:
            yield c
        return
    from app.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://in-process", timeout=90.0
        ) as c:
            yield c


async def _run(
    base_url: str, claims: list[dict], concurrency: int, use_llm: bool, in_process: bool = False
) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)
    async with _client(base_url, in_process) as client:

        async def one(item: dict) -> dict:
            async with sem:
                started = time.perf_counter()
                try:
                    resp = await client.post(
                        "/v1/verify",
                        json={"claim": item["claim"], "lang": item.get("lang"), "use_llm": use_llm},
                    )
                    body = resp.json() if resp.content else {}
                    verdict = body.get("verdict") if resp.status_code == 200 else None
                    err = None if resp.status_code == 200 else f"http_{resp.status_code}"
                except Exception as exc:  # report, never crash the whole run
                    body, verdict, err = {}, None, type(exc).__name__
                return {
                    "id": item["id"],
                    "claim": item["claim"],
                    "truth": item["truth"],
                    "kind": item.get("kind"),
                    "verdict": verdict,
                    "confidence": body.get("confidence"),
                    "independent_domains": body.get("independent_domains"),
                    "error": err,
                    "ms": int((time.perf_counter() - started) * 1000),
                }

        return await asyncio.gather(*(one(c) for c in claims))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--golden", type=Path, default=_GOLDEN)
    p.add_argument("--report-dir", type=Path, default=Path("evals/reports/verify"))
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--use-llm", action="store_true")
    p.add_argument(
        "--in-process", action="store_true", help="run the app in this process (no server)"
    )
    args = p.parse_args(argv)
    gold = json.loads(args.golden.read_text(encoding="utf-8"))["claims"]
    rows = asyncio.run(_run(args.base_url, gold, args.concurrency, args.use_llm, args.in_process))
    summary = score(gold, {r["id"]: r["verdict"] for r in rows})
    report = {
        "created": datetime.now(UTC).isoformat(),
        "golden": str(args.golden.as_posix()),
        "use_llm": args.use_llm,
        "summary": summary,
        "results": rows,
    }
    args.report_dir.mkdir(parents=True, exist_ok=True)
    out = args.report_dir / f"eval_verify_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(out), "summary": summary}, ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
