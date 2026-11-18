"""Market endpoints (docs/18 v2.3.0 Wave B).

POST /v1/fa/compare      -> cross-source fundamentals for 1..N symbols
POST /v1/market/ohlcv    -> daily candles (VNDIRECT primary, SSI fallback)

Both reuse the process-wide FAAggregator (deps.get_fa_aggregator) so FA
breakers persist across requests, and never invent values: fields/candles
carry source + as_of from the payload, missing data stays missing.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import asdict
from dataclasses import replace as _dc_replace
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Response

from app.api import deps
from app.api.v1.schemas import (
    CorporateEventOut,
    FaCompareRequest,
    FaCompareResponse,
    FaFieldOut,
    FaSymbolResult,
    MarketEventsRequest,
    MarketEventsResponse,
    MarketNewsWireItemOut,
    MarketNewsWireRequest,
    MarketNewsWireResponse,
    MarketOhlcvRequest,
    MarketOhlcvResponse,
    MarketSentimentRequest,
    MarketSentimentResponse,
    OhlcvCandle,
    ShareRatioExplanationOut,
)
from app.config import get_settings
from app.core.admission import admit
from app.core.budget import create_budget_for_route, use_budget
from app.core.errors import ErrorCode, MetaSearchError
from app.core.logging import get_logger
from app.retrieval.adapters.viet_stock import fetch_vndirect_ohlcv_series
from app.retrieval.fa_sources.base import ensure_global_symbol, ensure_symbol
from app.retrieval.sentiment_quant import (
    FundamentalSnapshotInput,
    MarketProxiesInput,
    OhlcvBarInput,
    fetch_live_market_proxies,
    fetch_sentiment_quant_context,
)
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
    # Registry records key the name as `company_name` (DAT-06) — `name` never
    # exists, so /v1/fa/compare served name=null for every symbol (round 10).
    name = str(rec.get("company_name") or "").strip() or None
    exchange = str(rec.get("exchange") or "").strip() or None
    return name, exchange


def _csv_safe_cell(val: object) -> str:
    """Neutralize spreadsheet formula injection (=, +, -, @, tab, cr) on non-numeric strings."""
    if isinstance(val, (int, float)):
        return str(val)
    text = "" if val is None else str(val)
    if text and text.lstrip()[0:1] in ("=", "+", "-", "@", "\t", "\r"):
        return f"'{text}"
    if text.startswith(("\t", "\r")):
        return f"'{text}"
    return text


def _compare_to_csv(results: list[FaSymbolResult]) -> str:
    """Phase 4 P0-1: CSV export of the compare table (docs/23 #1).

    UTF-8 BOM so Excel opens Vietnamese names correctly; empty cell for
    missing fields (no fabrication); a provenance column per symbol."""
    import csv as _csv
    import io as _io

    metric_names: list[str] = []
    for r in results:
        for k in r.fields:
            if k not in metric_names:
                metric_names.append(k)

    buf = _io.StringIO()
    writer = _csv.writer(buf, lineterminator="\n")
    writer.writerow(["metric"] + [_csv_safe_cell(r.symbol) for r in results])
    writer.writerow(["name"] + [_csv_safe_cell(r.name or "") for r in results])
    writer.writerow(["exchange"] + [_csv_safe_cell(r.exchange or "") for r in results])
    for metric in metric_names:
        row = [_csv_safe_cell(metric)]
        for r in results:
            fv = r.fields.get(metric)
            row.append("" if fv is None else _csv_safe_cell(fv.value))
        writer.writerow(row)
    writer.writerow(["sources"] + [_csv_safe_cell(",".join(r.sources_used)) for r in results])
    writer.writerow(["as_of"] + [_csv_safe_cell(r.as_of or "") for r in results])
    return "﻿" + buf.getvalue()


@router.post("/fa/compare", response_model=FaCompareResponse, response_model_exclude_none=True)
async def fa_compare(request: FaCompareRequest) -> FaCompareResponse | Response:
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

    if request.format not in ("json", "csv"):
        raise MetaSearchError(
            ErrorCode.INVALID_REQUEST,
            f"unsupported format: {request.format!r} (json | csv)",
            status_code=400,
        )

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
                    # docs/29 ISSUE-3: distinguish "all sources dead/disabled"
                    # (infrastructure) from "ticker unknown / no coverage".
                    all_sources_unavailable=True,
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
                elif res.fields == {} and not res.sources_used:
                    # docs/29 ISSUE-3: a 200 with zero fields and zero sources
                    # means every fundamentals source was dead/disabled — say
                    # so instead of silently handing over an empty table.
                    logger.warning("fa_compare_symbol_all_sources_unavailable", symbol=sym)
                    results.append(res.model_copy(update={"all_sources_unavailable": True}))
                else:
                    results.append(res)

    if request.format == "csv":
        return Response(
            content=_compare_to_csv(results),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=fa-compare.csv"},
        )
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
                    primary_t0 = time.perf_counter()
                    try:
                        series = await fetch_vndirect_ohlcv_series(
                            sym, days=days, timeout_s=timeout_s
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        series = None
                    if series is None:
                        # Remaining-budget rule (audit round 10): the fallback
                        # leg gets what the wall clock still allows, mirroring
                        # the search path — never a fresh full timeout after a
                        # slow primary.
                        elapsed = time.perf_counter() - primary_t0
                        fallback_timeout = min(
                            timeout_s,
                            max(0.5, timeout_s * 2 + 2.0 - elapsed - 0.25),
                        )
                        # Fallback: direct FA sources (SSI first) through the shared
                        # aggregator — same fan-out the search adapter uses (docs/15 §8.2).
                        # Guarded: any failure here is "no data" (503 below), never a 500.
                        try:
                            src = await deps.get_fa_aggregator().fetch_ohlcv(
                                sym, timeout_s=fallback_timeout
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


def _merge_live_market_proxies(
    base: MarketProxiesInput | None, live: MarketProxiesInput | None
) -> MarketProxiesInput | None:
    if live is None:
        return base
    if base is None:
        return live
    return _dc_replace(
        base,
        vix=base.vix if base.vix is not None else live.vix,
        vix_ma50=base.vix_ma50 if base.vix_ma50 is not None else live.vix_ma50,
        vix_3m=base.vix_3m if base.vix_3m is not None else live.vix_3m,
        source_note=base.source_note or live.source_note,
    )


def _parse_iso_dt(raw: str | None) -> datetime | None:
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


@router.post(
    "/market/sentiment",
    response_model=MarketSentimentResponse,
    response_model_exclude_none=False,
)
async def market_sentiment(request: MarketSentimentRequest) -> MarketSentimentResponse:
    """Omni-Channel Bilingual Equity Sentiment Engine (VI / EN) — Phase 1..3."""
    from app.orchestration.pipeline import SearchParams
    from app.retrieval.sentiment import (
        RawSentimentHit,
        analyze_omni_channel_sentiment,
        build_omni_channel_queries,
        resolve_equity_entity,
    )

    sym = ensure_symbol(request.ticker)
    if sym is None:
        raise MetaSearchError(
            ErrorCode.INVALID_REQUEST,
            f"invalid ticker: {request.ticker!r}",
            status_code=400,
        )

    entity = resolve_equity_entity(
        sym,
        company_name=request.company_name,
        exchange=request.exchange,
    )
    budget = create_budget_for_route("market_sentiment", timeout_ms=request.timeout_ms)
    total_timeout = budget.remaining() if budget else 15.0

    async with admit("market"):
        with use_budget(budget):
            hits: list[RawSentimentHit] = []
            # Execution Rule: If retrieved_documents are already provided in context,
            # bypass Phase 1 search fan-out and proceed directly to Phase 2.
            if request.retrieved_documents is not None:
                for doc in request.retrieved_documents:
                    hits.append(
                        RawSentimentHit(
                            url=doc.url,
                            title=doc.title,
                            snippet=doc.snippet,
                            content=doc.content,
                            published_at=_parse_iso_dt(doc.published_at),
                            lang=doc.lang,
                            client_supplied=True,
                        )
                    )
            else:
                queries = build_omni_channel_queries(entity, max_queries=request.max_queries)
                pipe = deps.get_search_pipeline()
                sem = asyncio.Semaphore(4)

                async def _run_vec(q_vec: Any) -> list[RawSentimentHit]:
                    async with sem:
                        try:
                            outcome = await pipe.run(
                                SearchParams(
                                    query=q_vec.query,
                                    lang=q_vec.lang.lower(),
                                    k=request.k,
                                    categories=[q_vec.category],
                                    record_memory=False,
                                    # PASS-2 audit P1-3: 22 site:-prefixed
                                    # sub-queries must not each pay the
                                    # adaptive-router deep escalation or LLM
                                    # query expansion (~10x cost amplifier).
                                    skip_router=True,
                                    expansion=False,
                                )
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            logger.warning(
                                "sentiment_fanout_query_failed",
                                ticker=sym,
                                layer=q_vec.layer,
                                error=type(exc).__name__,
                            )
                            return []
                        out: list[RawSentimentHit] = []
                        for r in outcome.results:
                            out.append(
                                RawSentimentHit(
                                    url=r.url,
                                    title=r.title,
                                    snippet=r.snippet,
                                    published_at=r.published_at,
                                    lang=q_vec.lang,
                                )
                            )
                        return out

                tasks = [asyncio.create_task(_run_vec(q)) for q in queries]
                try:
                    done, pending = await asyncio.wait(tasks, timeout=max(1.5, total_timeout))
                except BaseException:
                    for t in tasks:
                        t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
                for p_task in pending:
                    p_task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                batches: list[list[RawSentimentHit] | BaseException] = []
                for t in tasks:
                    if t not in done:
                        continue
                    try:
                        batches.append(t.result())
                    except BaseException as exc:
                        batches.append(exc)

                seen_urls: set[str] = set()
                for b in batches:
                    if isinstance(b, BaseException):
                        continue
                    for h in b:
                        if h.url and h.url not in seen_urls:
                            seen_urls.add(h.url)
                            hits.append(h)

            ohlcv_bars: list[OhlcvBarInput] | None = None
            ohlcv_src: str | None = None
            ohlcv_url: str | None = None
            ohlcv_as_of: str | None = None
            if request.ohlcv_bars:
                ohlcv_bars = [
                    OhlcvBarInput(
                        time=bar.time,
                        open=bar.open,
                        high=bar.high,
                        low=bar.low,
                        close=bar.close,
                        volume=bar.volume,
                    )
                    for bar in request.ohlcv_bars
                ]
                ohlcv_src = "request"
                ohlcv_as_of = datetime.fromtimestamp(
                    max(bar.time for bar in ohlcv_bars), tz=UTC
                ).isoformat()

            market_proxies: MarketProxiesInput | None = None
            if request.market_proxies is not None:
                mp = request.market_proxies
                mp_dict = mp.model_dump(exclude={"rana_series"})
                market_proxies = MarketProxiesInput(
                    **mp_dict,
                    rana_series=tuple(mp.rana_series) if mp.rana_series is not None else None,
                )

            fundamentals: FundamentalSnapshotInput | None = None
            quant_skipped_budget = False
            # docs/29 ISSUE-4: per-ticker isolation — VN tickers get their own
            # VNDIRECT OHLCV/quant by default (proven live, keyless) unless the
            # caller explicitly opts out via `"fetch_market_data": false`.
            explicit_fetch_off = (
                "fetch_market_data" in request.model_fields_set and not request.fetch_market_data
            )
            effective_fetch = request.fetch_market_data or (
                entity.is_vietnam and not explicit_fetch_off
            )
            if effective_fetch:
                rem = budget.remaining() if budget else 4.0
                if rem >= 0.8:
                    need_vix = not entity.is_vietnam and (
                        market_proxies is None
                        or market_proxies.vix is None
                        or market_proxies.vix_3m is None
                    )
                    quant_coro = fetch_sentiment_quant_context(
                        entity.ticker,
                        is_vietnam=entity.is_vietnam,
                        need_ohlcv=not bool(ohlcv_bars),
                        fa_aggregator=deps.get_fa_aggregator(),
                        timeout_s=min(4.0, rem),
                    )
                    if need_vix:
                        (quant_res, live_proxies) = await asyncio.gather(
                            quant_coro,
                            fetch_live_market_proxies(
                                entity.ticker,
                                is_vietnam=entity.is_vietnam,
                                timeout_s=min(3.0, rem),
                            ),
                        )
                        market_proxies = _merge_live_market_proxies(market_proxies, live_proxies)
                    else:
                        quant_res = await quant_coro
                    live_bars, live_src, live_url, live_as_of, fa_snap = quant_res
                    if not ohlcv_bars and live_bars:
                        ohlcv_bars = live_bars
                        ohlcv_src = live_src
                        ohlcv_url = live_url
                        ohlcv_as_of = live_as_of
                    fundamentals = fa_snap
                else:
                    quant_skipped_budget = True

            # PASS-2 audit P2-7: the lexicon scan + quant aggregation are
            # CPU-bound regex/scan work (measured 206 ms at schema-max
            # input) — offload to a worker thread, never stall the loop.
            payload = await asyncio.to_thread(
                analyze_omni_channel_sentiment,
                entity,
                hits,
                ohlcv_bars=ohlcv_bars,
                ohlcv_source_name=ohlcv_src,
                ohlcv_source_url=ohlcv_url,
                ohlcv_as_of=ohlcv_as_of,
                market_proxies=market_proxies,
                fundamentals=fundamentals,
            )
            if quant_skipped_budget:
                breakers_list = list(payload.get("risk_circuit_breakers") or [])
                if "QUANT_FETCH_SKIPPED_BUDGET" not in breakers_list:
                    breakers_list.append("QUANT_FETCH_SKIPPED_BUDGET")
                payload["risk_circuit_breakers"] = breakers_list
            return MarketSentimentResponse.model_validate(payload)


def _normalize_market_symbols(raw_symbols: list[str]) -> list[str]:
    out: list[str] = []
    for raw in raw_symbols:
        sym = ensure_symbol(raw) or ensure_global_symbol(raw)
        if sym is None:
            raise MetaSearchError(
                ErrorCode.INVALID_REQUEST,
                f"invalid symbol: {raw!r}",
                status_code=400,
            )
        if sym not in out:
            out.append(sym)
    return out


@router.post(
    "/market/news-wire",
    response_model=MarketNewsWireResponse,
    response_model_exclude_none=True,
)
async def market_news_wire(request: MarketNewsWireRequest) -> MarketNewsWireResponse:
    """Headless bilingual market news wire: RSS/HTML aggregation, entity disambiguation & dedup."""
    from app.retrieval.events import fetch_market_news_wire

    symbols = _normalize_market_symbols(request.symbols)
    budget = create_budget_for_route("market_news_wire", timeout_ms=request.timeout_ms)
    started = time.perf_counter()

    async with admit("market"):
        with use_budget(budget):
            rem = budget.remaining() if budget else 8.0
            if rem <= 0.1:
                raise MetaSearchError(
                    ErrorCode.DEADLINE_EXCEEDED,
                    "request budget exhausted before market_news_wire dispatch",
                    status_code=504,
                )
            timeout_s = min(12.0, rem)
            raw_articles = (
                [art.model_dump() for art in request.raw_articles]
                if request.raw_articles is not None
                else None
            )
            res = await fetch_market_news_wire(
                symbols=symbols or None,
                lang=request.lang,
                categories=request.categories,
                include_market_macro=request.include_market_macro,
                deduplicate=request.deduplicate,
                max_age_days=request.max_age_days,
                max_items=request.max_items,
                seen_fingerprints=request.seen_fingerprints or None,
                raw_articles=raw_articles,
                include_search_fallback=request.include_search_fallback,
                timeout_s=timeout_s,
                breakers=deps.get_events_breaker_registry(),
                cache=deps.get_cache(),
            )
            items_out = [
                MarketNewsWireItemOut(
                    title=it.title,
                    url=it.url,
                    source_id=it.source_id,
                    snippet=it.snippet,
                    published_at=_iso(it.published_at),
                    event_date=it.event_date,
                    symbols=list(it.symbols),
                    scope=it.scope,
                    category=it.category,
                    severity=it.severity,
                    stable_id=it.stable_id,
                    fingerprint=it.fingerprint,
                    date_status=it.date_status,
                    duplicate_count=it.duplicate_count,
                    also_reported_by=list(it.also_reported_by),
                    is_new=it.is_new,
                )
                for it in res["items"]
            ]
            return MarketNewsWireResponse(
                items=items_out,
                new_count=int(res["new_count"]),
                total_raw=int(res["total_raw"]),
                deduplicated_count=int(res["deduplicated_count"]),
                sources_used=list(res["sources_used"]),
                sources_failed=list(res["sources_failed"]),
                warnings=list(res["warnings"]),
                partial=bool(res["partial"]),
                new_fingerprints=list(res["new_fingerprints"]),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )


@router.post(
    "/market/events",
    response_model=MarketEventsResponse,
    response_model_exclude_none=True,
)
async def market_events(request: MarketEventsRequest) -> MarketEventsResponse:
    """Structured corporate actions, ex-dividend calendar, risk notices & ratio proof."""
    from app.retrieval.events import fetch_market_events

    if (request.observed_shares_high is None) != (request.observed_shares_low is None):
        raise MetaSearchError(
            ErrorCode.INVALID_REQUEST,
            "observed_shares_high and observed_shares_low must both be provided together",
            status_code=400,
        )

    symbols = _normalize_market_symbols(request.symbols)
    reported_ratio = request.reported_share_ratio
    if (
        reported_ratio is None
        and request.observed_shares_high is not None
        and request.observed_shares_low is not None
    ):
        reported_ratio = request.observed_shares_high / request.observed_shares_low
    if reported_ratio is not None and (
        not math.isfinite(reported_ratio) or reported_ratio < 1e-4 or reported_ratio > 1000.0
    ):
        raise MetaSearchError(
            ErrorCode.INVALID_REQUEST,
            "share ratio out of supported finite bounds (1e-4 .. 1000.0)",
            status_code=400,
        )

    budget = create_budget_for_route("market_events", timeout_ms=request.timeout_ms)
    started = time.perf_counter()

    async with admit("market"):
        with use_budget(budget):
            rem = budget.remaining() if budget else 10.0
            if rem <= 0.1:
                raise MetaSearchError(
                    ErrorCode.DEADLINE_EXCEEDED,
                    "request budget exhausted before market_events dispatch",
                    status_code=504,
                )
            timeout_s = min(14.0, rem)
            raw_notices = (
                [n.model_dump() for n in request.raw_notices]
                if request.raw_notices is not None
                else None
            )
            res = await fetch_market_events(
                symbols=symbols or None,
                include_corporate_actions=request.include_corporate_actions,
                include_calendar=request.include_calendar,
                include_listing=request.include_listing,
                include_insider=request.include_insider,
                include_regulatory=request.include_regulatory,
                lookback_days=request.lookback_days,
                lookahead_days=request.lookahead_days,
                reported_share_ratio=reported_ratio,
                ratio_tolerance=request.ratio_tolerance,
                seen_event_ids=request.seen_event_ids or None,
                raw_notices=raw_notices,
                timeout_s=timeout_s,
                breakers=deps.get_events_breaker_registry(),
                cache=deps.get_cache(),
            )
            ratio_exp = res.get("share_ratio_explanation")
            return MarketEventsResponse(
                symbols=list(res["symbols"]),
                events=[CorporateEventOut(**asdict(e)) for e in res["events"]],
                upcoming_ex_dates=[
                    CorporateEventOut(**asdict(e)) for e in res["upcoming_ex_dates"]
                ],
                dilution_alerts=[CorporateEventOut(**asdict(e)) for e in res["dilution_alerts"]],
                risk_alerts=[CorporateEventOut(**asdict(e)) for e in res["risk_alerts"]],
                share_ratio_explanation=(
                    ShareRatioExplanationOut(**asdict(ratio_exp)) if ratio_exp is not None else None
                ),
                new_events_count=int(res["new_events_count"]),
                new_event_ids=list(res["new_event_ids"]),
                sources_used=list(res["sources_used"]),
                sources_failed=list(res["sources_failed"]),
                warnings=list(res["warnings"]),
                partial=bool(res["partial"]),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
