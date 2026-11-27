"""Rate-based engine quarantine with a recovery probe (docs/36 S3, v4.7.0).

Why: the in-memory breaker re-tries a dead upstream every <=15 min forever, the
legacy ``auto:`` disable counts only *consecutive* failures (which stop being
counted once the breaker is open) and is wiped at every start-up
(``clear_legacy_auto_disabled``). Production data 2026-11-19..25: ddg succeeded
1/37 times, bluesky/reddit 0/12, gdelt 0/9 — none was ever disabled or alerted.

What: every ``engine_quarantine_interval_s`` the API process reads the
persisted per-minute ``engine_health`` windows and quarantines an ENABLED engine
when BOTH hold:

- evidence: the shortest window (24h, else 7d) with >= ``engine_quarantine_min_calls``
  calls has a success rate < ``engine_quarantine_max_success``;
- freshness: the last 24h holds >= ``engine_quarantine_min_recent_calls`` calls and
  is still failing at that rate (an engine an operator just re-enabled is judged
  only on calls made AFTER that re-enable).

Thresholds were replayed on the production health history of 2026-11-19..25
(a few searches/day): ddg, bluesky and reddit are quarantined, healthy engines
(sec_edgar 91/100, google_news 163/165) are not.

Quarantine = ``enabled=false`` + ``disabled_reason="quarantine:..."`` with the
numbers that justified it. The ``quarantine:`` prefix is NOT cleared at
start-up (only legacy ``auto:`` is), so the state survives restarts. A
quarantined engine is probed every ``engine_quarantine_probe_s``; a probe that
completes without an adapter error releases it (an empty result is fine — many
vertical engines legitimately return nothing for a generic probe query).
Operators can always re-enable by ``PATCH /v1/engines/{id}``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.logging import get_logger
from app.core.metrics import ENGINE_QUARANTINED, ENGINE_SUCCESS_RATE_24H

logger = get_logger(__name__)

QUARANTINE_PREFIX = "quarantine:"
# Set on an ENABLED engine when it leaves quarantine (probe or operator re-enable):
# "released:<ISO-8601>". It survives restarts, so failures recorded before the
# release are never judged again after a reboot (the in-memory mark alone was lost).
RELEASE_PREFIX = "released:"


def release_marker(now: datetime) -> str:
    return f"{RELEASE_PREFIX}{now.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}"


def released_at(reason: str | None) -> datetime | None:
    if not reason or not reason.startswith(RELEASE_PREFIX):
        return None
    try:
        return datetime.strptime(reason[len(RELEASE_PREFIX) :], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
    except ValueError:
        return None


_PROBE_QUERY = "metasearch recovery probe"


@dataclass(frozen=True, slots=True)
class EngineRates:
    calls_6h: int = 0
    successes_6h: int = 0
    calls_24h: int = 0
    successes_24h: int = 0
    calls_7d: int = 0
    successes_7d: int = 0

    @staticmethod
    def _rate(s: int, c: int) -> float | None:
        return round(s / c, 4) if c > 0 else None

    @property
    def success_rate_6h(self) -> float | None:
        return self._rate(self.successes_6h, self.calls_6h)

    @property
    def success_rate_24h(self) -> float | None:
        return self._rate(self.successes_24h, self.calls_24h)

    @property
    def success_rate_7d(self) -> float | None:
        return self._rate(self.successes_7d, self.calls_7d)


def is_quarantined(reason: str | None) -> bool:
    return bool(reason) and str(reason).startswith(QUARANTINE_PREFIX)


def quarantine_reason(rates: EngineRates, settings: Any) -> str | None:
    """Pure decision: the reason string when the engine must be quarantined.

    Evidence = the SHORTEST window (24h, then 7d) holding >= min_calls calls —
    a low-traffic deployment (a few searches a day) still accumulates evidence
    over the week. Freshness = the last 24h holds >= min_recent calls and is
    still below the threshold (an engine that started working again, or that
    nobody called lately, is never quarantined on stale data)."""
    min_calls = int(getattr(settings, "engine_quarantine_min_calls", 8))
    min_recent = int(getattr(settings, "engine_quarantine_min_recent_calls", 2))
    max_ok = float(getattr(settings, "engine_quarantine_max_success", 0.2))
    if rates.calls_24h >= min_calls:
        window, calls, succ = "24h", rates.calls_24h, rates.successes_24h
    elif rates.calls_7d >= min_calls:
        window, calls, succ = "7d", rates.calls_7d, rates.successes_7d
    else:
        return None
    if succ / calls >= max_ok:
        return None
    r24 = rates.success_rate_24h
    if rates.calls_24h < min_recent or r24 is None or r24 >= max_ok:
        return None
    head = f"{QUARANTINE_PREFIX}success_{window}={succ / calls:.2f} ({succ}/{calls})"
    if window == "24h":
        return head
    return f"{head}, success_24h={r24:.2f} ({rates.successes_24h}/{rates.calls_24h})"


def rates_from_rows(
    rows: list[tuple[str, datetime, int, int]],
    *,
    now: datetime,
    ignore_before: dict[str, datetime] | None = None,
) -> dict[str, EngineRates]:
    """Aggregate per-minute ``(engine_id, window_start, calls, successes)`` rows.

    ``ignore_before[engine]`` drops windows older than an operator re-enable so
    pre-release failures cannot immediately re-quarantine the engine."""
    t6, t24, t7 = now - timedelta(hours=6), now - timedelta(hours=24), now - timedelta(days=7)
    acc: dict[str, list[int]] = {}
    for engine_id, ws, calls, succ in rows:
        if ws.tzinfo is None:
            ws = ws.replace(tzinfo=UTC)
        cut = (ignore_before or {}).get(engine_id)
        if cut is not None and ws < cut:
            continue
        a = acc.setdefault(engine_id, [0, 0, 0, 0, 0, 0])
        c, s = int(calls or 0), int(succ or 0)
        if ws >= t7:
            a[4] += c
            a[5] += s
        if ws >= t24:
            a[2] += c
            a[3] += s
        if ws >= t6:
            a[0] += c
            a[1] += s
    return {e: EngineRates(*v) for e, v in acc.items()}


async def probe_engine(adapter: Any, *, timeout_s: float = 5.0) -> bool:
    """True when one bounded probe search completes without an adapter error."""
    if adapter is None:
        return False
    try:
        async with asyncio.timeout(timeout_s + 1.0):
            await adapter.search(_PROBE_QUERY, max_results=1, timeout_s=timeout_s)
        return True
    except Exception as exc:
        logger.info("engine_quarantine_probe_failed", error=type(exc).__name__)
        return False


class EngineQuarantine:
    """Periodic evaluator. One instance per API process (lifespan task)."""

    def __init__(self, store: Any, adapters: dict[str, Any], settings: Any) -> None:
        self._store = store
        self._adapters = adapters
        self._settings = settings
        self._last_probe: dict[str, float] = {}
        self._released_at: dict[str, datetime] = {}
        self._was_quarantined: set[str] = set()

    async def run_once(self, *, now: datetime | None = None) -> dict[str, list[str]]:
        now = now or datetime.now(UTC)
        out: dict[str, list[str]] = {"quarantined": [], "released": []}
        history = getattr(self._store, "history_rates", None)
        if history is None:
            return out  # in-memory store: no persisted history, nothing to judge
        configs = await self._store.snapshot()
        # Operator re-enabled an engine we had quarantined -> judge only newer calls.
        for eid in list(self._was_quarantined):
            cfg = configs.get(eid)
            if cfg is not None and cfg.enabled and not is_quarantined(cfg.disabled_reason):
                self._released_at.setdefault(eid, now)
                self._was_quarantined.discard(eid)
        ignore_before = dict(self._released_at)
        for eid, cfg in configs.items():
            persisted = released_at(cfg.disabled_reason) if cfg.enabled else None
            if persisted is not None and persisted > ignore_before.get(eid, persisted):
                ignore_before[eid] = persisted
            elif persisted is not None:
                ignore_before.setdefault(eid, persisted)
        rates = await history(now=now, ignore_before=ignore_before)
        for eid, cfg in configs.items():
            r = rates.get(eid, EngineRates())
            if r.success_rate_24h is not None:
                ENGINE_SUCCESS_RATE_24H.labels(engine=eid).set(r.success_rate_24h)
            if is_quarantined(cfg.disabled_reason):
                self._was_quarantined.add(eid)
                ENGINE_QUARANTINED.labels(engine=eid).set(1)
                if await self._maybe_release(eid, now):
                    out["released"].append(eid)
                continue
            ENGINE_QUARANTINED.labels(engine=eid).set(0)
            if not cfg.enabled:
                continue  # disabled by operator or missing key: not ours to judge
            reason = quarantine_reason(r, self._settings)
            if reason is None:
                continue
            reason = f"{reason} since {now.strftime('%Y-%m-%dT%H:%MZ')}"
            if await self._store.quarantine(eid, reason):
                self._was_quarantined.add(eid)
                self._released_at.pop(eid, None)
                self._last_probe[eid] = time.monotonic()
                ENGINE_QUARANTINED.labels(engine=eid).set(1)
                logger.warning("engine_quarantined", engine=eid, reason=reason)
                out["quarantined"].append(eid)
        return out

    async def _maybe_release(self, eid: str, now: datetime) -> bool:
        interval = float(getattr(self._settings, "engine_quarantine_probe_s", 3600))
        last = self._last_probe.get(eid)
        if last is not None and time.monotonic() - last < interval:
            return False
        self._last_probe[eid] = time.monotonic()
        if not await probe_engine(self._adapters.get(eid)):
            return False
        if await self._store.release(eid):
            self._released_at[eid] = now
            self._was_quarantined.discard(eid)
            ENGINE_QUARANTINED.labels(engine=eid).set(0)
            logger.info("engine_quarantine_released", engine=eid)
            return True
        return False

    async def loop(self, stop: asyncio.Event) -> None:
        interval = float(getattr(self._settings, "engine_quarantine_interval_s", 600))
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception as exc:  # fail-open: never take the API down
                logger.warning("engine_quarantine_tick_failed", error=type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
