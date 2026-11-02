"""Runs the REAL MetaSearch app from the worktree for the benchmark.

Applies two test-rig patches:
  * SSRF resolves loopback as allowed so the fake pages on 127.0.0.1 can be scraped.
  * Five general engines point at the fake SearXNG (real adapter code, real client creation).

Also adds:
  * GET /__lag: longest event-loop stall since the last read.
  * GET /__rss: current process RSS memory in megabytes.

Usage:
  python -m scripts.perf.server --profile memory
  python -m scripts.perf.server --profile lite
  python -m scripts.perf.server --profile postgres
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
from contextlib import asynccontextmanager, suppress


def get_current_rss_mb() -> float:
    """Get current process RSS in MB across Windows and POSIX."""
    if sys.platform == "win32":
        try:
            import ctypes.wintypes

            class PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.wintypes.DWORD),
                    ("PageFaultCount", ctypes.wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            psapi = ctypes.windll.psapi
            kernel32 = ctypes.windll.kernel32
            psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.wintypes.HANDLE,
                ctypes.POINTER(PMC),
                ctypes.wintypes.DWORD,
            ]
            psapi.GetProcessMemoryInfo.restype = ctypes.wintypes.BOOL
            kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE

            counters = PMC()
            counters.cb = ctypes.sizeof(PMC)
            if psapi.GetProcessMemoryInfo(
                kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
            ):
                return counters.WorkingSetSize / (1024 * 1024)
        except Exception:
            return 0.0
    else:
        try:
            import resource

            scale = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / scale
        except Exception:
            return 0.0
    return 0.0


def setup_env(profile: str, upstream_url: str) -> None:
    tmp = tempfile.mkdtemp(prefix="ms_bench_")
    env = {
        "ENVIRONMENT": "dev",
        "LOG_LEVEL": "ERROR",
        "SEARXNG_URL": upstream_url,
        "SEARXNG_TIMEOUT_S": "3",
        "CRAWL4AI_URL": "http://127.0.0.1:1",
        "JINA_KEYLESS": "false",
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp}/bench.db",
        "LLM_PROVIDER": "custom",
        "CUSTOM_LLM_BASE_URL": f"{upstream_url}/v1",
        "CUSTOM_LLM_API_KEY": "bench",
        "LLM_QUICK_MODEL": "bench-model",
        "ENGINE_AUTO_DISABLE_THRESHOLD": "99999",
        "TAVILY_API_KEY": "x",
        "SERPER_API_KEY": "x",
        "BRAVE_API_KEY": "x",
    }
    if profile == "memory":
        env.update(
            ENGINE_STORE_BACKEND="memory",
            CACHE_ENABLED="false",
            REDIS_URL="redis://127.0.0.1:6399/0",
        )
    elif profile == "lite":
        env.update(
            ENGINE_STORE_BACKEND="db",
            CACHE_ENABLED="true",
            REDIS_URL="redis://127.0.0.1:6399/0",
        )
    elif profile == "postgres":
        env.update(
            ENGINE_STORE_BACKEND="db",
            CACHE_ENABLED="true",
            DATABASE_URL=os.environ.get(
                "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/metasearch"
            ),
            REDIS_URL=os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
        )
    else:
        raise ValueError(f"Unknown profile: {profile!r}. Expected memory | lite | postgres")

    os.environ.update(env)


def main() -> None:
    parser = argparse.ArgumentParser(description="MetaSearch benchmark server runner")
    parser.add_argument(
        "--profile",
        default="memory",
        choices=["memory", "lite", "postgres"],
        help="Server profile (default: memory)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=18765, help="Port to bind (default: 18765)")
    parser.add_argument(
        "--upstream-port",
        type=int,
        default=18766,
        help="Port of fake upstream (default: 18766)",
    )
    args = parser.parse_args()

    upstream_url = f"http://127.0.0.1:{args.upstream_port}"
    setup_env(args.profile, upstream_url)

    # Allow local worktree imports
    sys.path.insert(0, os.getcwd())

    import uvicorn
    from starlette.responses import JSONResponse

    import app.api.deps as deps
    from app.extraction import ssrf
    from app.retrieval import registry as reg

    # 1. Patch SSRF to allow fake upstreams on loopback
    orig_resolve = ssrf.resolve_and_check

    async def _loopback_ok(url: str) -> frozenset[str]:
        if f"127.0.0.1:{args.upstream_port}" in url:
            return frozenset({"127.0.0.1"})
        return await orig_resolve(url)

    ssrf.resolve_and_check = _loopback_ok

    # 2. Patch adapters to route 5 general engines to fake SearXNG
    def _adapters(settings):
        from app.retrieval.adapters.searxng import SearXNGAdapter

        return {
            name: SearXNGAdapter(settings.searxng_url)
            for name in ("searxng", "ddg", "tavily", "serper", "brave")
        }

    reg.build_adapters = _adapters
    deps.build_adapters = _adapters

    from app.main import app as fastapp

    # 3. Event-loop stall monitor with lifespan management
    _stall = {"max": 0.0}

    async def _monitor():
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(0.01)
            _stall["max"] = max(_stall["max"], time.perf_counter() - t0 - 0.01)

    orig_lifespan = fastapp.router.lifespan_context

    @asynccontextmanager
    async def bench_lifespan(application):
        monitor_task = asyncio.create_task(_monitor())
        try:
            async with orig_lifespan(application):
                yield
        finally:
            monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await monitor_task

    fastapp.router.lifespan_context = bench_lifespan

    # 4. Diagnostic endpoints for benchmark load runner
    @fastapp.get("/__lag", include_in_schema=False)
    async def lag() -> JSONResponse:
        value = _stall["max"]
        _stall["max"] = 0.0
        return JSONResponse({"max_stall_ms": round(value * 1000, 1)})

    @fastapp.get("/__rss", include_in_schema=False)
    async def rss() -> JSONResponse:
        return JSONResponse({"rss_mb": round(get_current_rss_mb(), 2)})

    uvicorn.run(fastapp, host=args.host, port=args.port, log_level="error")


if __name__ == "__main__":
    main()
