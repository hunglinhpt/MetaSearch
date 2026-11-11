/**
 * Pure transforms for the OHLCV chart (docs/18 B2): backend candles ->
 * lightweight-charts series + headline stats. lightweight-charts expects
 * `time` as UNIX seconds (business-day strings need a calendar; the feed's
 * own epoch seconds are the grounded choice), so pass-through with validation.
 */

import type { OhlcvCandle, MarketOhlcvResponse } from "./api";

export type Candle = { time: number; open: number; high: number; low: number; close: number };
export type VolumeBar = { time: number; value: number; color: string };

export const UP_COLOR = "#26a69a";
export const DOWN_COLOR = "#ef5350";

/** Validate + map candles; drops non-finite OHLC (feed fault), then sorts by
 * time and dedupes — lightweight-charts THROWS on unsorted/duplicate time. */
export function toCandles(candles: OhlcvCandle[]): Candle[] {
  const byTime = new Map<number, Candle>();
  for (const c of candles) {
    if (
      !Number.isFinite(c.time) ||
      !Number.isFinite(c.open) ||
      !Number.isFinite(c.high) ||
      !Number.isFinite(c.low) ||
      !Number.isFinite(c.close)
    ) {
      continue;
    }
    byTime.set(c.time, { time: c.time, open: c.open, high: c.high, low: c.low, close: c.close });
  }
  return [...byTime.values()].sort((a, b) => a.time - b.time);
}

export function toVolumeBars(candles: OhlcvCandle[]): VolumeBar[] {
  const byTime = new Map<number, VolumeBar>();
  for (const c of candles) {
    // Same validity rule as toCandles: a candle with non-finite OHLC is a
    // feed fault and must not appear as a (wrongly colored) volume bar.
    if (
      !Number.isFinite(c.time) ||
      !Number.isFinite(c.volume) ||
      !Number.isFinite(c.open) ||
      !Number.isFinite(c.close)
    ) {
      continue;
    }
    byTime.set(c.time, {
      time: c.time,
      value: c.volume,
      color: c.close >= c.open ? UP_COLOR : DOWN_COLOR,
    });
  }
  return [...byTime.values()].sort((a, b) => a.time - b.time);
}

export type ChartStats = {
  last: number | null;
  first: number | null;
  changePct: number | null; // (last - first) / first * 100 over the window
  high: number | null;
  low: number | null;
  volumeTotal: number | null;
};

/** Headline stats computed ONLY from payload candles — never estimated. */
export function computeStats(candles: OhlcvCandle[]): ChartStats {
  const valid = candles.filter(
    (c) =>
      Number.isFinite(c.close) &&
      Number.isFinite(c.high) &&
      Number.isFinite(c.low) &&
      Number.isFinite(c.volume)
  );
  if (!valid.length) {
    return { last: null, first: null, changePct: null, high: null, low: null, volumeTotal: null };
  }
  const first = valid[0].close;
  const last = valid[valid.length - 1].close;
  return {
    last,
    first,
    changePct: first !== 0 ? ((last - first) / first) * 100 : null,
    high: Math.max(...valid.map((c) => c.high)),
    low: Math.min(...valid.map((c) => c.low)),
    volumeTotal: valid.reduce((acc, c) => acc + c.volume, 0),
  };
}

/** Format a VND price (already x1000-corrected by the backend) for display. */
export function formatVnd(v: number | null): string {
  if (v === null || !Number.isFinite(v)) return "—";
  return v.toLocaleString("vi-VN", { maximumFractionDigits: 0 });
}

export function formatPct(v: number | null): string {
  if (v === null || !Number.isFinite(v)) return "—";
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
}

export function formatVolume(v: number | null): string {
  if (v === null || !Number.isFinite(v)) return "—";
  if (v >= 1e9) return `${(v / 1e9).toFixed(2)} tỷ`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(2)} triệu`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)} nghìn`;
  return String(Math.round(v));
}

/** Response -> chart model in one call (used by ChartPanel). */
export function chartModel(resp: MarketOhlcvResponse): {
  candles: Candle[];
  volumes: VolumeBar[];
  stats: ChartStats;
} {
  return {
    candles: toCandles(resp.candles),
    volumes: toVolumeBars(resp.candles),
    stats: computeStats(resp.candles),
  };
}
