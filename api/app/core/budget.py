"""End-to-end request budget and deadline propagation (readiness WP-07)."""

from __future__ import annotations

import contextvars
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

# Context variable storing the active RequestBudget for the current async task
_current_budget: contextvars.ContextVar[RequestBudget | None] = contextvars.ContextVar(
    "_current_budget", default=None
)


class RequestBudget:
    """Carries end-to-end deadline and stage caps across the request pipeline."""

    def __init__(
        self,
        total_timeout_s: float,
        *,
        stage_caps: dict[str, float] | None = None,
        start_time: float | None = None,
    ) -> None:
        self.total_timeout_s = max(0.1, float(total_timeout_s))
        self.start_time = time.monotonic() if start_time is None else start_time
        self.deadline = self.start_time + self.total_timeout_s
        self.stage_caps = dict(stage_caps or {})

    def remaining(self) -> float:
        """Seconds remaining before overall request deadline."""
        return max(0.0, self.deadline - time.monotonic())

    def is_expired(self) -> bool:
        return self.remaining() <= 0.0

    def stage(self, name: str, default_cap: float | None = None) -> float:
        """Effective timeout budget for a named stage (capped by overall remaining time)."""
        rem = self.remaining()
        cap = self.stage_caps.get(name, default_cap)
        if cap is None:
            return rem
        return max(0.0, min(float(cap), rem))


def get_current_budget() -> RequestBudget | None:
    return _current_budget.get()


@contextmanager
def use_budget(budget: RequestBudget) -> Iterator[RequestBudget]:
    token = _current_budget.set(budget)
    try:
        yield budget
    finally:
        _current_budget.reset(token)


def current_timeout(default_s: float, stage_name: str | None = None) -> float:
    """Helper for HTTP/I/O calls: min(configured_default, remaining_budget)."""
    b = get_current_budget()
    if b is None:
        return default_s
    rem = b.stage(stage_name) if stage_name else b.remaining()
    return max(0.05, min(default_s, rem))


def create_budget_for_route(
    route: str,
    *,
    timeout_ms: int | None = None,
    settings: Any = None,
) -> RequestBudget:
    """Factory creating RequestBudget with standard route defaults or explicit timeout_ms."""
    # Standard route defaults per readiness WP-07
    # /v1/search: total 5.0s, search 3.5s
    # /v1/context: total 12.0s, search 3.5s, scrape 6.0s
    # /v1/answer: total 12.0s to first token, search 3.5s, scrape 5.0s
    # /v1/scrape: total 8.0s, scrape 8.0s
    if timeout_ms is not None:
        total_s = float(timeout_ms) / 1000.0
        # Scale stage caps if total is smaller than standard
        search_cap = min(3.5, total_s * 0.7)
        scrape_cap = min(6.0, total_s * 0.8)
    elif route == "search":
        total_s = 5.0
        search_cap = 3.5
        scrape_cap = 0.0
    elif route == "context":
        total_s = 12.0
        search_cap = 3.5
        scrape_cap = 6.0
    elif route == "answer":
        total_s = 12.0
        search_cap = 3.5
        scrape_cap = 5.0
    elif route == "scrape":
        total_s = 8.0
        search_cap = 0.0
        scrape_cap = 8.0
    else:
        total_s = 15.0
        search_cap = 5.0
        scrape_cap = 8.0

    stage_caps = {"search": search_cap, "scrape": scrape_cap}
    return RequestBudget(total_s, stage_caps=stage_caps)
