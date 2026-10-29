"""MetaSearch performance check — micro benchmarks + API load test.

Runs without Docker: a canned SearXNG is served locally and the real app
stack (uvicorn, in-process) is exercised end to end.

    python scripts/perf_load.py

Environment pinned inside the script BEFORE app import (settings are cached):
fake engine on 127.0.0.1:9999, cache disabled, Postgres pointed at a port
that refuses connections instantly so fallback paths run, not hang.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ["ENVIRONMENT"] = "dev"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["SEARXNG_URL"] = "http://127.0.0.1:9999"
os.environ["CACHE_ENABLED"] = "false"
# Nothing listens here: connect is refused in milliseconds, exercising the
# same fallback paths a dead-DB production box would take.
os.environ["DATABASE_URL"] = "postgresql+asyncpg://x:x@127.0.0.1:5432/x"
os.environ["REDIS_URL"] = "redis://127.0.0.1:6399/0"
# Perf run measures the pipeline, not the resilience policy: bursts against
# the overloaded fake engine must not trip auto-disable mid-test.
os.environ["ENGINE_AUTO_DISABLE_THRESHOLD"] = "999"
# Pure pipeline numbers: skip the Postgres-backed engine store entirely.
os.environ["ENGINE_STORE_BACKEND"] = "memory"

API = "http://127.0.0.1:8001"
FAKE_SEARXNG_PORT = 9999

FAKE_RESULTS = [
    {
        "url": f"https://site{i}.example.com/postgres/article-{i}",
        "title": f"PostgreSQL tuning guide part {i}",
        "content": "PostgreSQL performance tuning covers indexes, vacuum, "
        "connections and query planning for production workloads.",
        "engines": ["google", "bing"] if i % 2 else ["ddg"],
        "score": float(20 - i),
    }
    for i in range(15)
]


class FakeSearXNG(BaseHTTPRequestHandler):
    # Default backlog (5) refuses connections under concurrent load and would
    # fabricate engine errors that are artifacts of the test rig.
    request_queue_size = 128
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path.startswith("/search"):
            body = json.dumps(
                {"query": "perf", "results": FAKE_RESULTS, "answers": [], "infoboxes": []}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/healthz"):
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args) -> None:  # silence
        pass


def start_fake_searxng() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", FAKE_SEARXNG_PORT), FakeSearXNG)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def start_api() -> threading.Thread:
    import uvicorn

    from app.main import app as fastapp  # import AFTER env vars are set

    config = uvicorn.Config(fastapp, host="127.0.0.1", port=8001, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return thread


def wait_ready() -> None:
    import httpx

    for _ in range(100):
        try:
            if httpx.get(f"{API}/healthz", timeout=1).status_code == 200:
                return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("API did not come up")


async def load(url: str, *, n: int, concurrency: int, payload: dict | None = None) -> dict:
    import httpx

    latencies: list[float] = []
    errors = 0
    statuses: dict[int, int] = {}
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(timeout=30) as client:

        async def one() -> None:
            nonlocal errors
            async with sem:
                started = time.perf_counter()
                try:
                    if payload is not None:
                        resp = await client.post(url, json=payload)
                    else:
                        resp = await client.get(url)
                    statuses[resp.status_code] = statuses.get(resp.status_code, 0) + 1
                    if resp.status_code >= 500:
                        errors += 1
                except Exception:
                    errors += 1
                latencies.append((time.perf_counter() - started) * 1000)

        await asyncio.gather(*(one() for _ in range(n)))

    latencies.sort()

    def pct(q: float) -> float:
        idx = min(int(len(latencies) * q), len(latencies) - 1)
        return latencies[idx]

    return {
        "n": len(latencies),
        "concurrency": concurrency,
        "errors": errors,
        "statuses": statuses,
        "p50_ms": pct(0.50),
        "p95_ms": pct(0.95),
        "p99_ms": pct(0.99),
        "rps": len(latencies) / (sum(latencies) / 1000) * concurrency if latencies else 0.0,
    }


def micro_benchmarks() -> list[tuple[str, float, str]]:
    from datetime import UTC, datetime

    from app.extraction.chunker import chunk_markdown
    from app.retrieval.canonical import canonicalize
    from app.retrieval.merger import merge
    from app.retrieval.models import EngineConfig, RawResult
    from app.retrieval.scoring import heuristic_chunk_score
    from app.retrieval.simhash import simhash64

    registry = {"searxng": EngineConfig(id="searxng", name="s", category="general")}
    raws = [
        RawResult(
            url=f"https://site{i}.example.com/a?utm_source=x&id={i}",
            title=f"PostgreSQL tuning part {i}",
            snippet="indexes vacuum connections query planning production workloads",
            adapter="searxng",
            source_engines=["google", "bing"] if i % 2 else ["ddg"],
            rank=i,
        )
        for i in range(500)
    ]
    now = datetime.now(UTC)
    results: list[tuple[str, float, str]] = []

    started = time.perf_counter()
    for _ in range(20):
        merge(raws, query="postgres tuning", registry=registry, now=now)
    results.append(
        ("merge+score 500 raws (20 runs)", (time.perf_counter() - started) / 20 * 1000, "ms")
    )

    texts = [f"postgres tuning guide part {i} indexes vacuum connections" for i in range(300)]
    started = time.perf_counter()
    for text in texts:
        simhash64(text)
    results.append((f"simhash x{len(texts)}", (time.perf_counter() - started) * 1000, "ms"))

    urls = [f"https://www.site{i}.example.com/a?utm_source=x&fbclid=y&id={i}" for i in range(5000)]
    started = time.perf_counter()
    for u in urls:
        canonicalize(u)
    results.append((f"canonicalize x{len(urls)}", (time.perf_counter() - started) * 1000, "ms"))

    markdown = (
        "# Heading\n\n" + "lorem ipsum dolor sit amet consectetur adipiscing elit " * 40
    ) * 60
    started = time.perf_counter()
    chunks = chunk_markdown(markdown, document_url="https://x.com", source_title="T")
    results.append(
        (
            f"chunker {len(markdown) // 1024}KB doc → {len(chunks)} chunks",
            (time.perf_counter() - started) * 1000,
            "ms",
        )
    )

    started = time.perf_counter()
    for chunk in chunks[:50]:
        heuristic_chunk_score("postgres tuning", chunk.content, 0.8, chunk.position)
    results.append(("heuristic chunk score x50", (time.perf_counter() - started) * 1000, "ms"))
    return results


async def main() -> int:
    print("=" * 72)
    print("MetaSearch performance check (no Docker needed)")
    print("=" * 72)

    print("\n-- Micro benchmarks (hot paths) --")
    for name, value, unit in micro_benchmarks():
        print(f"   {name:<48} {value:8.1f} {unit}")

    print("\n-- API load test (real pipeline: fanout -> merge -> score -> JSON) --")
    start_fake_searxng()
    start_api()
    wait_ready()

    scenarios = [
        ("GET  /healthz", f"{API}/healthz", None, 400, 20),
        (
            "POST /v1/search k=10 (c=1)",
            f"{API}/v1/search",
            {"query": "postgres tuning", "k": 10},
            30,
            1,
        ),
        (
            "POST /v1/search k=10 (c=5)",
            f"{API}/v1/search",
            {"query": "postgres tuning", "k": 10},
            60,
            5,
        ),
        (
            "POST /v1/search k=10 (c=25)",
            f"{API}/v1/search",
            {"query": "postgres tuning", "k": 10},
            200,
            25,
        ),
    ]
    for name, url, payload, n, conc in scenarios:
        stats = await load(url, n=n, concurrency=conc, payload=payload)
        line = (
            f"   {name:<32} n={stats['n']:<4} c={stats['concurrency']:<3} "
            f"err={stats['errors']:<3} p50={stats['p50_ms']:7.1f}ms "
            f"p95={stats['p95_ms']:7.1f}ms p99={stats['p99_ms']:7.1f}ms "
            f"~{stats['rps']:6.0f} rps  {stats['statuses']}"
        )
        print(line, flush=True)

    print(
        "\n   Note: engine-health writes go through the Postgres fallback "
        "(refused\n   instantly) and cache is disabled — numbers reflect the "
        "pure pipeline."
    )
    print("   KPI references (docs/12): raw search p95 <= 2.5s, engine success >= 95%.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
