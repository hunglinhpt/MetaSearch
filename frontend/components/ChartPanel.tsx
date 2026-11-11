"use client";

/**
 * Candlestick chart for /v1/market/ohlcv (docs/18 B2). lightweight-charts
 * (Apache-2.0, TradingView) is loaded lazily inside useEffect so SSR never
 * touches it; theme colors come from the CSS variables in globals.css.
 */

import { useEffect, useRef, useState } from "react";

import type { IChartApi, Time } from "lightweight-charts";

import { marketOhlcv, safeHref, type MarketOhlcvResponse } from "../lib/api";
import {
  chartModel,
  formatPct,
  formatVnd,
  formatVolume,
  DOWN_COLOR,
  UP_COLOR,
} from "../lib/chartData";

const DAY_LABELS: Record<number, string> = { 30: "30 ngày", 90: "90 ngày", 180: "180 ngày", 365: "1 năm" };

export default function ChartPanel({ symbol }: { symbol: string }) {
  const [days, setDays] = useState<number>(90);
  const [data, setData] = useState<MarketOhlcvResponse | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!symbol.trim()) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    setError("");
    marketOhlcv(symbol.trim().toUpperCase(), days, controller.signal)
      .then((d) => {
        if (!controller.signal.aborted) setData(d);
      })
      .catch((e) => {
        if (controller.signal.aborted) return;
        setData(null);
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [symbol, days]);

  // (Re)draw the chart whenever data or container changes.
  useEffect(() => {
    const container = containerRef.current;
    if (!container || !data) return;
    let disposed = false;
    let chart: IChartApi | null = null;
    (async () => {
      try {
        // lightweight-charts v5 API: addSeries(CandlestickSeries, opts) — the
        // v4 addCandlestickSeries/addHistogramSeries shortcuts were REMOVED in
        // v5, so using them is a runtime TypeError (caught below, chart blank).
        const lwc = await import("lightweight-charts");
        if (disposed || !containerRef.current) return;
        const model = chartModel(data);
        chart = lwc.createChart(container, {
          width: container.clientWidth,
          height: 320,
          layout: {
            background: { color: "transparent" },
            textColor: "#9aa7b4",
            fontFamily: "inherit",
          },
          grid: {
            vertLines: { color: "#223040" },
            horzLines: { color: "#223040" },
          },
          rightPriceScale: { borderColor: "#223040" },
          timeScale: { borderColor: "#223040", timeVisible: true, secondsVisible: false },
          autoSize: true,
        });
        const candle = chart.addSeries(lwc.CandlestickSeries, {
          upColor: UP_COLOR,
          downColor: DOWN_COLOR,
          wickUpColor: UP_COLOR,
          wickDownColor: DOWN_COLOR,
          borderVisible: false,
        });
        candle.setData(model.candles.map((c) => ({ ...c, time: c.time as Time })));
        const volume = chart.addSeries(lwc.HistogramSeries, {
          priceFormat: { type: "volume" },
          priceScaleId: "volume",
        });
        chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
        volume.setData(model.volumes.map((v) => ({ ...v, time: v.time as Time })));
      } catch (e) {
        // lightweight-charts throws on bad data — surface it instead of a
        // blank panel with an unhandled rejection.
        if (!disposed) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      disposed = true;
      if (chart) chart.remove();
      chart = null;
    };
  }, [data]);

  const stats = data ? chartModel(data).stats : null;

  return (
    <div className="chart-panel">
      <div className="chart-toolbar">
        <strong>{symbol.toUpperCase()}</strong>
        {data && (
          <span className="chart-source">
            nguồn:{" "}
            <a href={safeHref(data.source_url)} target="_blank" rel="noreferrer">
              {data.source_name}
            </a>
            {data.as_of && <> · mốc: {new Date(data.as_of).toLocaleDateString("vi-VN")}</>}
          </span>
        )}
        <span className="chart-days">
          {[30, 90, 180, 365].map((d) => (
            <button
              key={d}
              type="button"
              className={`chip chip-toggle ${days === d ? "on" : ""}`}
              onClick={() => setDays(d)}
              disabled={loading}
            >
              {DAY_LABELS[d]}
            </button>
          ))}
        </span>
      </div>
      {stats && (
        <div className="chart-stats">
          <span>Giá cuối: <strong>{formatVnd(stats.last)}</strong></span>
          <span>Biến động: <strong>{formatPct(stats.changePct)}</strong></span>
          <span>Cao: {formatVnd(stats.high)}</span>
          <span>Thấp: {formatVnd(stats.low)}</span>
          <span>KL: {formatVolume(stats.volumeTotal)}</span>
        </div>
      )}
      {loading && <div className="card status"><div className="spinner" /><span>Đang tải nến...</span></div>}
      {error && <div className="card error">{error}</div>}
      <div ref={containerRef} className="chart-container" style={{ minHeight: 320 }} />
    </div>
  );
}
