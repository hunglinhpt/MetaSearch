"""Market endpoints (docs/18 v2.3.0 Wave B).

POST /v1/fa/compare      -> cross-source fundamentals for 1..N symbols
POST /v1/market/ohlcv    -> daily candles (VNDIRECT primary, SSI fallback)

Both reuse the process-wide FAAggregator (deps.get_fa_aggregator) so FA
breakers persist across requests, and never invent values: fields/candles
carry source + as_of from the payload, missing data stays missing.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter

from app.api import deps
from app.api.v1.schemas import (
    FaCompareRequest,
    FaCompareResponse,
    FaFieldOut,
    FaSymbolResult,
    MarketOhlcvRequest,
    MarketOhlcvResponse,
    OhlcvCandle,
)
from app.config import get_settings
from app.core.admission import admit
from app.core.budget import create_budget_for_route, use_budget
from app.core.errors import ErrorCode, MetaSearchError
from app.core.logging import get_logger
from app.retrieval.adapters.viet_stock import fetch_vndirect_ohlcv_series
from app.retrieval.fa_sources.base import ensure_symbol
from app.retrieval.vn_tickers import load_vn_tickers

router = APIRouter(tags=["market"])
logger = get_logger(__name__)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _ticker_meta(sym: str) -> tuple[str | None, str | None]:
    """Name/exchange from the DAT-06 registry; unknown codes stay None."""
    rec = load_vn_tickers().get(sym) or {}
    name = str(rec.get("name") or "").strip() or None
    exchange = str(rec.get("exchange") or "").strip() or None
    return name, exchange


@router.post("/fa/compare", response_model=FaCompareResponse, response_model_exclude_none=True)
async def fa_compare(request: FaCompareRequest) -> FaCompareResponse:
    settings = get_settings()
    cap = settings.fa_compare_max_symbols
    if len(request.symbols) > cap:
        raise MetaSearchError(
            ErrorCode.INVALID_REQUEST,
            f"too many symbols: {len(request.symbols)} > cap {cap}",
            status_code=400,
            details={"cap": cap},
        )
    symbols: list[str] = []
    for raw in request.symbols:
        sym = ensure_symbol(raw)
        if sym is None:
            raise MetaSearchError(
                ErrorCode.INVALID_REQUEST,
                f"invalid symbol: {raw!r}",
                status_code=400,
            )
        if sym not in symbols:
            symbols.append(sym)
    if not symbols:
        raise MetaSearchError(ErrorCode.INVALID_REQUEST, "no valid symbols", status_code=400)

    budget = create_budget_for_route("fa_compare", timeout_ms=request.timeout_ms)
    aggregator = deps.get_fa_aggregator()
    remaining = budget.remaining() if budget else settings.fa_compare_timeout_s
    timeout_s = min(settings.fa_compare_timeout_s, remaining)
    started = time.perf_counter()

    async with admit("market"):
        with use_budget(budget):
            # One fan-out per symbol, all symbols concurrently, under a single
            # wall-clock cap so a slow source cannot stretch the request.
            async def one(sym: str) -> FaSymbolResult:
                agg = await aggregator.fetch_financials(sym, timeout_s=timeout_s)
                name, exchange = _ticker_meta(sym)
                return FaSymbolResult(
                    symbol=sym,
                    name=name,
                    exchange=exchange,
                    fields={
                        k: FaFieldOut(
                            value=fv.value,
                            source=fv.source,
                            as_of=_iso(fv.as_of),
                            validated=fv.validated,
                        )
                        for k, fv in agg.fields.items()
                    },
                    missing_fields=list(agg.missing_fields),
                    sources_used=list(agg.sources_used),
                    source_urls=list(agg.source_urls),
                    as_of=_iso(agg.as_of),
                    published_at=_iso(agg.published_at),
                )

            def empty(sym: str, why: str) -> FaSymbolResult:
                """Per-symbol failure entry — a broken symbol must not sink the
                whole table; every canonical field is honestly missing."""
                from app.retrieval.fa_sources.base import CANONICAL_FA_FIELDS

                logger.warning("fa_compare_symbol_empty", symbol=sym, reason=why)
                name, exchange = _ticker_meta(sym)
                return FaSymbolResult(
                    symbol=sym,
                    name=name,
                    exchange=exchange,
                    fields={},
                    missing_fields=list(CANONICAL_FA_FIELDS),
                    sources_used=[],
                    source_urls=[],
                    as_of=None,
                    published_at=None,
                )

            # All symbols fan out CONCURRENTLY (each with its own per-source
            # timeout), so the wall clock is one timeout + slack, not n x it.
            try:
                async with asyncio.timeout(timeout_s + 2.0):
                    gathered = await asyncio.gather(
                        *(one(s) for s in symbols), return_exceptions=True
                    )
            except TimeoutError as exc:
                # Error model: a blown wall clock is a deadline, not a 500.
                raise MetaSearchError(
                    ErrorCode.DEADLINE_EXCEEDED,
                    f"fa/compare deadline exceeded after {timeout_s + 2.0:.1f}s",
                    status_code=504,
                    details={"symbols": symbols},
                ) from exc
            results: list[FaSymbolResult] = []
            for sym, res in zip(symbols, gathered, strict=True):
                if isinstance(res, BaseException):
                    logger.warning("fa_compare_symbol_failed", symbol=sym, error=type(res).__name__)
                    results.append(empty(sym, type(res).__name__))
                else:
                    results.append(res)

    return FaCompareResponse(
        results=results,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


@router.post("/market/ohlcv", response_model=MarketOhlcvResponse, response_model_exclude_none=True)
async def market_ohlcv(request: MarketOhlcvRequest) -> MarketOhlcvResponse:
    settings = get_settings()
    sym = ensure_symbol(request.symbol)
    if sym is None:
        raise MetaSearchError(
            ErrorCode.INVALID_REQUEST, f"invalid symbol: {request.symbol!r}", status_code=400
        )
    days = request.days if request.days is not None else settings.market_ohlcv_default_days
    days = max(settings.market_ohlcv_min_days, min(settings.market_ohlcv_max_days, days))

    budget = create_budget_for_route("market_ohlcv", timeout_ms=request.timeout_ms)
    remaining = budget.remaining() if budget else settings.market_ohlcv_timeout_s
    timeout_s = min(settings.market_ohlcv_timeout_s, remaining)
    started = time.perf_counter()

    async with admit("market"):
        with use_budget(budget):
            # Primary: VNDIRECT dchart (full series incl. per-candle times).
            series: dict[str, Any] | None = None
            source_name = "VNDIRECT"
            source_url = f"https://dchart.vndirect.com.vn/?symbol={sym}"
            # One wall-clock cap over BOTH legs (primary then fallback run
            # sequentially), so a dead primary cannot stretch the request.
            deadline_hit = False
            try:
                async with asyncio.timeout(timeout_s * 2 + 2.0):
                    try:
                        series = await fetch_vndirect_ohlcv_series(
                            sym, days=days, timeout_s=timeout_s
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        series = None
                    if series is None:
                        # Fallback: direct FA sources (SSI first) through the shared
                        # aggregator — same fan-out the search adapter uses (docs/15 §8.2).
                        # Guarded: any failure here is "no data" (503 below), never a 500.
                        try:
                            src = await deps.get_fa_aggregator().fetch_ohlcv(
                                sym, timeout_s=timeout_s
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            logger.warning(
                                "market_ohlcv_fallback_failed", symbol=sym, error=type(exc).__name__
                            )
                            src = None
                        if src is not None and src.n:
                            series = {
                                "times": src.times,
                                "opens": src.opens,
                                "highs": src.highs,
                                "lows": src.lows,
                                "closes": src.closes,
                                "volumes": src.volumes,
                                "last_ts": src.last_ts,
                            }
                            source_name = src.source_id.upper()
                            # Provenance honesty: only claim the fallback's own
                            # URL — never label SSI data with the VNDIRECT link.
                            source_url = src.source_url
            except TimeoutError:
                # A blown wall clock is a deadline, not "no data" — report it as
                # such (same contract as fa/compare), never as a bare 503.
                deadline_hit = True
                series = None

    if deadline_hit:
        raise MetaSearchError(
            ErrorCode.DEADLINE_EXCEEDED,
            f"market/ohlcv deadline exceeded for {sym}",
            status_code=504,
            details={"symbol": sym, "days": days},
        )
    if not series or not series.get("closes"):
        raise MetaSearchError(
            ErrorCode.NO_RESULTS,
            f"no OHLCV data for {sym} from any source",
            status_code=503,
            details={"symbol": sym, "days": days},
        )

    # Defense in depth: build candles with zip(strict) so any array-length
    # drift from a feed can never raise IndexError — it refuses instead.
    # Times are NEVER synthesized: a source without per-candle timestamps
    # yields no chart (503 below), because a fabricated 86400-step calendar
    # would mislabel candles on weekends/holidays.
    times = series.get("times") or []
    closes = series["closes"]
    opens, highs, lows, volumes = (
        series["opens"],
        series["highs"],
        series["lows"],
        series["volumes"],
    )
    if not times or not (
        len(times) == len(closes) == len(opens) == len(highs) == len(lows) == len(volumes)
    ):
        raise MetaSearchError(
            ErrorCode.NO_RESULTS,
            f"OHLCV source for {sym} returned a series without usable timestamps",
            status_code=503,
            details={"symbol": sym, "source": source_name, "n": len(closes)},
        )
    candles = [
        OhlcvCandle(
            time=int(t),
            open=float(op),
            high=float(hi),
            low=float(lo),
            close=float(cl),
            volume=float(vo),
        )
        for t, op, hi, lo, cl, vo in zip(times, opens, highs, lows, closes, volumes, strict=True)
    ]
    # Trim to the requested window (the feed may return a bit more).
    candles = candles[-days:] if len(candles) > days else candles
    last_ts = int(series.get("last_ts") or candles[-1].time)
    return MarketOhlcvResponse(
        symbol=sym,
        candles=candles,
        source_name=source_name,
        source_url=source_url,
        as_of=datetime.fromtimestamp(last_ts, tz=UTC).isoformat(),
        days=days,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
