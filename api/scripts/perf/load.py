"""Load driver for MetaSearch benchmark.

Unique queries force cache misses; repeated queries hit.
Measures latency percentiles, RPS, event-loop stalls, TTFT, and process RSS memory.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import platform
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18765
DEFAULT_UPSTREAM_PORT = 18766


async def _check_token(client: httpx.AsyncClient, api_url: str) -> None:
    """Only measure the bench server this machine started on that port (scripts/perf/server.py)."""
    port = urlsplit(api_url).port
    expected_file = Path(tempfile.gettempdir()) / f"metasearch_bench_{port}.token"
    expected = expected_file.read_text(encoding="ascii").strip() if expected_file.exists() else None
    got = (await client.get(f"{api_url}/__rss")).json().get("token")
    if not expected or got != expected:
        raise RuntimeError(
            f"{api_url} is not the bench server started by scripts/perf/server.py "
            "(token mismatch); refusing to measure it"
        )


async def wait_ready(api_url: str, max_attempts: int = 120, delay_s: float = 0.25) -> None:
    async with httpx.AsyncClient(timeout=2) as client:
        for _ in range(max_attempts):
            try:
                resp_health = await client.get(f"{api_url}/healthz")
                if resp_health.status_code == 200:
                    resp_meta = await client.get(f"{api_url}/v1/meta")
                    if resp_meta.status_code == 200:
                        data = resp_meta.json()
                        app_name = data.get("name")
                        await _check_token(client, api_url)
                        if app_name != "MetaSearch AI":
                            raise RuntimeError(
                                f"Foreign service on {api_url}: "
                                f"expected 'MetaSearch AI', got {app_name!r}"
                            )
                        return
            except RuntimeError:
                raise
            except Exception:
                await asyncio.sleep(delay_s)
    raise SystemExit(f"API at {api_url} not ready within timeout")


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    sorted_vals = sorted(values)
    idx = min(int(len(sorted_vals) * q), len(sorted_vals) - 1)
    return sorted_vals[idx]


async def run_scenario(
    api_url: str,
    path: str,
    body_fn,
    *,
    n: int,
    conc: int,
    label: str,
    stream: bool = False,
) -> dict:
    lat: list[float] = []
    ttft: list[float] = []
    statuses: dict[str, int] = {}
    errors = 0
    sem = asyncio.Semaphore(conc)
    limits = httpx.Limits(max_connections=conc + 5, max_keepalive_connections=conc + 5)

    async with httpx.AsyncClient(timeout=120, limits=limits) as client:
        with contextlib.suppress(Exception):
            await client.get(f"{api_url}/__lag")

        async def one() -> None:
            nonlocal errors
            async with sem:
                t0 = time.perf_counter()
                try:
                    payload = body_fn() if callable(body_fn) else body_fn
                    if stream:
                        async with client.stream("POST", f"{api_url}{path}", json=payload) as resp:
                            first_token_time = None
                            async for line in resp.aiter_lines():
                                if first_token_time is None and line.startswith("event: token"):
                                    first_token_time = time.perf_counter() - t0
                            status_key = str(resp.status_code)
                            statuses[status_key] = statuses.get(status_key, 0) + 1
                            if resp.status_code >= 500:
                                errors += 1
                            if first_token_time is not None:
                                ttft.append(first_token_time * 1000)
                    else:
                        resp = await client.post(f"{api_url}{path}", json=payload)
                        status_key = str(resp.status_code)
                        statuses[status_key] = statuses.get(status_key, 0) + 1
                        if resp.status_code >= 500:
                            errors += 1
                except Exception as exc:
                    exc_name = type(exc).__name__
                    statuses[exc_name] = statuses.get(exc_name, 0) + 1
                    errors += 1
                lat.append((time.perf_counter() - t0) * 1000)

        wall0 = time.perf_counter()
        await asyncio.gather(*(one() for _ in range(n)))
        wall = time.perf_counter() - wall0

        stall_max_ms = 0.0
        with contextlib.suppress(Exception):
            stall_resp = await client.get(f"{api_url}/__lag")
            if stall_resp.status_code == 200:
                stall_max_ms = stall_resp.json().get("max_stall_ms", 0.0)

    p50 = pct(lat, 0.50)
    p95 = pct(lat, 0.95)
    p99 = pct(lat, 0.99)
    rps = n / wall if wall > 0 else 0.0

    result = {
        "label": label,
        "path": path,
        "n": n,
        "concurrency": conc,
        "p50_ms": round(p50, 1),
        "p95_ms": round(p95, 1),
        "p99_ms": round(p99, 1),
        "rps": round(rps, 2),
        "stall_max_ms": round(stall_max_ms, 1),
        "errors": errors,
        "statuses": statuses,
    }

    extra = ""
    if ttft:
        result["ttft_p50_ms"] = round(pct(ttft, 0.50), 1)
        result["ttft_p95_ms"] = round(pct(ttft, 0.95), 1)
        extra = f" ttft_p50={result['ttft_p50_ms']:.0f}ms ttft_p95={result['ttft_p95_ms']:.0f}ms"

    print(
        f"{label:<34} n={n:<4} c={conc:<3} p50={p50:7.0f}ms p95={p95:7.0f}ms "
        f"rps={rps:6.1f} stall_max={stall_max_ms:6.0f}ms{extra} {json.dumps(statuses)}",
        flush=True,
    )
    return result


def uniq_body(prefix: str, **extra):
    return lambda: {"query": f"{prefix} {uuid.uuid4().hex[:8]}", "k": 10, **extra}


async def run_benchmark(args: argparse.Namespace) -> dict:
    api_url = f"http://{args.host}:{args.port}"
    upstream_url = f"http://{args.host}:{args.upstream_port}"

    print(f"Waiting for MetaSearch API at {api_url}...")
    await wait_ready(api_url)
    print("API is ready. Starting benchmark load...")

    rss_start_mb = 0.0
    async with httpx.AsyncClient(timeout=5) as client:
        with contextlib.suppress(Exception):
            r = await client.get(f"{api_url}/__rss")
            if r.status_code == 200:
                rss_start_mb = r.json().get("rss_mb", 0.0)

        with contextlib.suppress(Exception):
            await client.get(f"{upstream_url}/__counts")

    results: list[dict] = []

    # 1. Search scenarios
    if args.scenario in ("all", "search"):
        for conc, n in ((1, 8), (5, 30), (20, 80)):
            res = await run_scenario(
                api_url,
                "/v1/search",
                uniq_body("hqc tim kiem"),
                n=n,
                conc=conc,
                label="search miss, 5 engines",
            )
            results.append(res)

        res = await run_scenario(
            api_url,
            "/v1/search",
            lambda: {"query": "hqc cau hoi lap lai", "k": 10},
            n=40,
            conc=10,
            label="search same query",
        )
        results.append(res)

    # 2. Context scenarios (external caller path)
    if args.scenario in ("all", "context"):
        for conc, n in ((1, 4), (5, 15), (10, 20), (20, 40)):
            res = await run_scenario(
                api_url,
                "/v1/context",
                uniq_body("hqc ngu canh"),
                n=n,
                conc=conc,
                label="context (external caller path), k=10",
            )
            results.append(res)

    # 3. Answer SSE scenarios
    if args.scenario in ("all", "answer"):
        for conc, n in ((1, 4), (5, 15), (15, 30)):
            res = await run_scenario(
                api_url,
                "/v1/answer",
                uniq_body("hqc tra loi", stream=True),
                n=n,
                conc=conc,
                label="answer SSE, k=10",
                stream=True,
            )
            results.append(res)

        res = await run_scenario(
            api_url,
            "/v1/answer",
            lambda: {"query": "hqc cung cau hoi dong thoi", "k": 10},
            n=10,
            conc=10,
            label="answer stampede same query",
        )
        results.append(res)

    # Collect final RSS and upstream counts
    rss_end_mb = 0.0
    upstream_counts: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=5) as client:
        with contextlib.suppress(Exception):
            r = await client.get(f"{api_url}/__rss")
            if r.status_code == 200:
                rss_end_mb = r.json().get("rss_mb", 0.0)

        with contextlib.suppress(Exception):
            r = await client.get(f"{upstream_url}/__counts")
            if r.status_code == 200:
                upstream_counts = r.json()

    return {
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "profile": args.profile,
        "timestamp": datetime.now(UTC).isoformat(),
        "rss_start_mb": round(rss_start_mb, 2),
        "rss_end_mb": round(rss_end_mb, 2),
        "upstream_calls": upstream_counts,
        "scenarios": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="MetaSearch benchmark load runner")
    parser.add_argument(
        "--profile",
        default="memory",
        choices=["memory", "lite", "postgres"],
        help="Profile tested (default: memory)",
    )
    parser.add_argument(
        "--scenario",
        default="all",
        choices=["all", "search", "context", "answer"],
        help="Scenarios to run (default: all)",
    )
    parser.add_argument("--out", help="Path to write JSON benchmark output")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Target host (default: 127.0.0.1)")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="Target port (default: 18765)"
    )
    parser.add_argument(
        "--upstream-port",
        type=int,
        default=DEFAULT_UPSTREAM_PORT,
        help="Fake upstream port (default: 18766)",
    )
    args = parser.parse_args()

    summary = asyncio.run(run_benchmark(args))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        print(f"\nSaved benchmark results to {args.out}")


if __name__ == "__main__":
    main()
