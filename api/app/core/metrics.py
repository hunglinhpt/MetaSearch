"""Prometheus metrics registry (see docs/09-observability-eval.md)."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

REGISTRY = CollectorRegistry(auto_describe=True)

HTTP_REQUESTS = Counter(
    "metasearch_http_requests_total",
    "HTTP requests handled",
    labelnames=("method", "path", "status"),
    registry=REGISTRY,
)

HTTP_LATENCY = Histogram(
    "metasearch_http_request_duration_seconds",
    "HTTP request latency",
    labelnames=("method", "path"),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 12, 20, 30),
    registry=REGISTRY,
)

ENGINE_CALLS = Counter(
    "metasearch_engine_calls_total",
    "Upstream search engine calls",
    labelnames=("engine", "outcome"),
    registry=REGISTRY,
)

ENGINE_LATENCY = Histogram(
    "metasearch_engine_duration_seconds",
    "Upstream search engine latency",
    labelnames=("engine",),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8),
    registry=REGISTRY,
)

# WP-12: single cache counter with a `kind` label so PromQL can compute
# per-kind hit rates (search, answer, page, negative, semantic).
CACHE_EVENTS = Counter(
    "metasearch_cache_events_total",
    "Cache hits and misses",
    labelnames=("kind", "outcome"),
    registry=REGISTRY,
)

LLM_TOKENS = Counter(
    "metasearch_llm_tokens_total",
    "LLM tokens consumed",
    labelnames=("model", "direction"),
    registry=REGISTRY,
)

LLM_COST = Counter(
    "metasearch_llm_cost_usd_total",
    "Estimated LLM spend in USD",
    labelnames=("model",),
    registry=REGISTRY,
)

READINESS = Gauge(
    "metasearch_readiness",
    "1 when the service is ready to serve traffic",
    registry=REGISTRY,
)

# WP-12 observability additions (docs/14 03-work-packages.md).
EVENT_LOOP_LAG = Histogram(
    "metasearch_event_loop_lag_seconds",
    "Event loop lag measured by a periodic probe",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
    registry=REGISTRY,
)
INFLIGHT_REQUESTS = Gauge(
    "metasearch_inflight_requests",
    "Requests currently in flight",
    labelnames=("route_class",),
    registry=REGISTRY,
)
STAGE_DURATION = Histogram(
    "metasearch_stage_duration_seconds",
    "Per-stage pipeline duration",
    labelnames=("stage",),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 180),
    registry=REGISTRY,
)
SCRAPE_TOTAL = Counter(
    "metasearch_scrape_total",
    "Scrape attempts by tier and outcome",
    labelnames=("tier", "outcome"),
    registry=REGISTRY,
)
LLM_REQUESTS = Counter(
    "metasearch_llm_requests_total",
    "LLM upstream calls by provider and outcome",
    labelnames=("provider", "outcome"),
    registry=REGISTRY,
)
LLM_TTFT = Histogram(
    "metasearch_llm_ttft_seconds",
    "Time to first LLM token",
    labelnames=("provider",),
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
    registry=REGISTRY,
)
HTTP_POOL_IN_USE = Gauge(
    "metasearch_http_pool_in_use",
    "Connections currently checked out of the shared HTTP pool",
    labelnames=("pool",),
    registry=REGISTRY,
)
BACKGROUND_QUEUE_DEPTH = Gauge(
    "metasearch_background_queue_depth",
    "Depth of internal background queues",
    labelnames=("queue",),
    registry=REGISTRY,
)
ADMISSION_REJECTED = Counter(
    "metasearch_admission_rejected_total",
    "Requests rejected by admission control",
    labelnames=("route_class",),
    registry=REGISTRY,
)
BREAKER_STATE = Gauge(
    "metasearch_breaker_state",
    "Circuit breaker state: 0=closed, 1=half_open, 2=open",
    labelnames=("engine",),
    registry=REGISTRY,
)
PROCESS_RSS = Gauge(
    "metasearch_process_rss_bytes",
    "Process resident set size in bytes",
    registry=REGISTRY,
)


def render_metrics() -> bytes:
    """Serialize the registry in Prometheus text exposition format."""
    return generate_latest(REGISTRY)


# ---------------------------------------------------------------- runtime sampling (WP-12)

_BREAKER_LEVEL = {"closed": 0, "half_open": 1, "open": 2}


def export_breaker_state(name: str, state: str) -> None:
    """Publish one breaker's state: 0 closed, 1 half-open, 2 open."""
    if name:
        BREAKER_STATE.labels(engine=name).set(_BREAKER_LEVEL.get(state, 0))


def process_rss_bytes() -> float | None:
    """Resident memory of this process, or None when the platform gives no cheap answer."""
    try:
        import psutil  # optional

        return float(psutil.Process().memory_info().rss)
    except ImportError:
        pass
    import sys

    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class _Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "PeakWorkingSetSize",
                    "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage",
                    "PagefileUsage",
                    "PeakPagefileUsage",
                )
            ]

        counters = _Counters()
        counters.cb = ctypes.sizeof(_Counters)
        kernel32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_Counters),
            wintypes.DWORD,
        ]
        if psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            return float(counters.WorkingSetSize)
        return None
    try:
        import os

        with open("/proc/self/statm", encoding="ascii") as fh:
            pages = int(fh.read().split()[1])
        return float(pages * os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError, IndexError):
        return None


def _sample_http_pools() -> None:
    # httpx exposes no public pool statistics; read the transport's pool, never fail on it.
    try:
        from typing import get_args

        from app import http as http_mod

        for name in get_args(http_mod.PoolName):
            if name not in http_mod._pools:
                HTTP_POOL_IN_USE.labels(pool=name).set(0)  # not created yet: nothing in use
        for name, client in list(http_mod._pools.items()):
            pool = getattr(getattr(client, "_transport", None), "_pool", None)
            connections = list(getattr(pool, "connections", []) or [])
            busy = sum(1 for c in connections if not c.is_idle())
            HTTP_POOL_IN_USE.labels(pool=name).set(busy)
    except Exception:
        return


async def runtime_monitor(interval_s: float = 0.5) -> None:
    """Sample event loop lag, RSS and HTTP pool usage until cancelled."""
    import asyncio

    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        await asyncio.sleep(interval_s)
        EVENT_LOOP_LAG.observe(max(0.0, loop.time() - started - interval_s))
        rss = process_rss_bytes()
        if rss is not None:
            PROCESS_RSS.set(rss)
        _sample_http_pools()
