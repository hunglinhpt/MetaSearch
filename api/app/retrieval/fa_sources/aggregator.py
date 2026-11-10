"""FAAggregator — fan-out to direct sources, cross-validate, merge (docs/15 §3).

Merge rules (§3.3):
- **Consensus**: a field whose value matches (rel. tolerance 0.5%) in >= 2
  sources is tagged ``validated=True``.
- **Conflict**: sources disagree -> highest priority wins, ``fa_conflict`` is
  logged with the per-source values.
- **Singleton**: field from one source only -> kept, ``validated=False``.
- **Missing**: field from no source -> absent from ``fields`` and listed in
  ``missing_fields``. Nothing is invented.

Every source is gated by an ``EngineBreaker`` named ``fa:{source_id}`` (state
exported as ``metasearch_breaker_state{engine="fa:..."}``); per-source fetches
run under their own timeout so one slow source cannot stretch the fan-out.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime

from app.retrieval.fa_sources.base import (
    CANONICAL_FA_FIELDS,
    FASource,
    FASourceError,
    SourceFinancials,
    SourceOHLCV,
)
from app.retrieval.health import EngineBreakerRegistry, Outcome

logger = logging.getLogger(__name__)

# Relative tolerance for "two sources agree" on the same ratio.
_CONSENSUS_REL_TOL = 0.005


@dataclass(slots=True)
class FieldValue:
    """One merged field with full provenance."""

    value: float
    source: str
    as_of: datetime | None
    validated: bool  # >= 2 sources agree within tolerance


@dataclass(slots=True)
class AggregatedFinancials:
    """Merged fundamental snapshot; every field carries source + as_of."""

    symbol: str
    fields: dict[str, FieldValue] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    sources_used: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    as_of: datetime | None = None  # latest grounded as_of across winners

    def get(self, name: str) -> float | None:
        fv = self.fields.get(name)
        return fv.value if fv else None


def _values_agree(a: float, b: float, rel_tol: float = _CONSENSUS_REL_TOL) -> bool:
    return math.isclose(a, b, rel_tol=rel_tol)


class FAAggregator:
    """Fan-out + cross-validate + merge across direct FA sources."""

    def __init__(
        self,
        sources: list[FASource],
        *,
        registry: EngineBreakerRegistry | None = None,
        timeout_s: float = 3.5,
    ) -> None:
        self._sources = sorted(sources, key=lambda s: s.priority, reverse=True)
        self._registry = registry or EngineBreakerRegistry()
        self._timeout_s = timeout_s

    # -- breaker helpers ---------------------------------------------------

    def _breaker_id(self, source: FASource) -> str:
        return f"fa:{source.source_id}"

    def _record(self, source: FASource, outcome: Outcome) -> None:
        self._registry.record(self._breaker_id(source), outcome)

    # -- financials ---------------------------------------------------------

    async def _fetch_one_financials(
        self, source: FASource, symbol: str, timeout_s: float
    ) -> tuple[SourceFinancials | None, Outcome]:
        """One source attempt; the caller records the outcome exactly ONCE.

        Recording here AND for the returned ``None`` would double-count a fault
        (the EMPTY record would reset the breaker's failure streak).
        """
        try:
            payload = await asyncio.wait_for(
                source.fetch_financials(symbol, timeout_s=timeout_s),
                timeout=timeout_s + 0.1,
            )
        except FASourceError as exc:
            logger.debug("fa_source_fault: %s %s", source.source_id, exc.detail)
            return None, exc.outcome
        except TimeoutError:
            return None, Outcome.TIMEOUT
        except Exception as exc:
            logger.debug("fa_source_unexpected: %s %s", source.source_id, exc)
            return None, Outcome.UPSTREAM_ERROR
        # A fault-free None means "healthy response, no data for this symbol".
        return payload, (Outcome.OK if payload is not None else Outcome.EMPTY)

    async def fetch_financials(
        self, symbol: str, *, timeout_s: float | None = None
    ) -> AggregatedFinancials:
        """Fan-out to every breaker-allowed source and merge by priority."""
        timeout = self._timeout_s if timeout_s is None else timeout_s

        eligible: list[FASource] = []
        skipped: list[str] = []
        for source in self._sources:
            if self._registry.allow(self._breaker_id(source)):
                eligible.append(source)
            else:
                skipped.append(source.source_id)
        if skipped:
            logger.debug("fa_sources_skipped_by_breaker: %s", skipped)

        ok: list[SourceFinancials] = []
        if eligible:
            results = await asyncio.gather(
                *(self._fetch_one_financials(source, symbol, timeout) for source in eligible),
                return_exceptions=False,
            )
            for source, (payload, outcome) in zip(eligible, results, strict=True):
                self._record(source, outcome)
                if payload is not None:
                    ok.append(payload)
        # Highest priority first (already sorted; re-sort defensively on id ties).
        ok.sort(key=lambda p: (-_priority_of(self._sources, p.source_id), p.source_id))

        return self._merge(symbol, ok)

    def _merge(self, symbol: str, payloads: list[SourceFinancials]) -> AggregatedFinancials:
        merged = AggregatedFinancials(symbol=symbol)
        merged.missing_fields = list(CANONICAL_FA_FIELDS)

        for payload in payloads:
            merged.sources_used.append(payload.source_id)
            if payload.source_url and payload.source_url not in merged.source_urls:
                merged.source_urls.append(payload.source_url)

            for name in CANONICAL_FA_FIELDS:
                value = payload.fields.get(name)
                if value is None:
                    continue
                if name not in merged.missing_fields:
                    # Already merged from a higher-priority source: use it for
                    # consensus checking, never overwrite the winner.
                    existing = merged.fields[name]
                    if _values_agree(existing.value, value):
                        existing.validated = True
                    else:
                        logger.warning(
                            "fa_conflict: %s %s winner=%s(%s) challenger=%s(%s)",
                            symbol,
                            name,
                            existing.value,
                            existing.source,
                            value,
                            payload.source_id,
                        )
                    continue
                merged.missing_fields.remove(name)
                merged.fields[name] = FieldValue(
                    value=value,
                    source=payload.source_id,
                    as_of=payload.as_of,
                    validated=False,
                )

        if merged.fields:
            merged.as_of = max(
                (fv.as_of for fv in merged.fields.values() if fv.as_of is not None),
                default=None,
            )
        return merged

    # -- ohlcv ----------------------------------------------------------------

    async def _fetch_one_ohlcv(
        self, source: FASource, symbol: str, timeout_s: float
    ) -> tuple[SourceOHLCV | None, Outcome]:
        """Same single-record contract as ``_fetch_one_financials``."""
        try:
            payload = await asyncio.wait_for(
                source.fetch_ohlcv(symbol, timeout_s=timeout_s), timeout=timeout_s + 0.1
            )
        except FASourceError as exc:
            logger.debug("fa_ohlcv_fault: %s %s", source.source_id, exc.detail)
            return None, exc.outcome
        except TimeoutError:
            return None, Outcome.TIMEOUT
        except Exception as exc:
            logger.debug("fa_ohlcv_unexpected: %s %s", source.source_id, exc)
            return None, Outcome.UPSTREAM_ERROR
        return payload, (Outcome.OK if payload is not None else Outcome.EMPTY)

    async def fetch_ohlcv(
        self, symbol: str, *, timeout_s: float | None = None
    ) -> SourceOHLCV | None:
        """First successful OHLCV in priority order (candles are canonical — no merge)."""
        timeout = self._timeout_s if timeout_s is None else timeout_s

        eligible = [s for s in self._sources if self._registry.allow(self._breaker_id(s))]
        winner: SourceOHLCV | None = None
        if eligible:
            results = await asyncio.gather(
                *(self._fetch_one_ohlcv(source, symbol, timeout) for source in eligible),
                return_exceptions=False,
            )
            for source, (payload, breaker_outcome) in zip(eligible, results, strict=True):
                self._record(source, breaker_outcome)
                if payload is not None and winner is None:
                    winner = payload
        return winner


def _priority_of(sources: list[FASource], source_id: str) -> float:
    for s in sources:
        if s.source_id == source_id:
            return s.priority
    return 0.0
