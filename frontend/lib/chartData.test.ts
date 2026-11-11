import { describe, expect, it } from "vitest";

import type { OhlcvCandle, MarketOhlcvResponse } from "./api";
import {
  chartModel,
  computeStats,
  formatPct,
  formatVnd,
  formatVolume,
  toCandles,
  toVolumeBars,
} from "./chartData";

const candles: OhlcvCandle[] = [
  { time: 1000, open: 100, high: 110, low: 95, close: 105, volume: 1000 },
  { time: 2000, open: 105, high: 108, low: 100, close: 102, volume: 2000 },
  { time: 3000, open: 102, high: 106, low: 101, close: 104, volume: 1500 },
];

describe("toCandles", () => {
  it("drops non-finite candles (feed fault) and keeps the rest", () => {
    const dirty = [...candles, { time: 4000, open: NaN, high: 1, low: 1, close: 1, volume: 1 }];
    const out = toCandles(dirty);
    expect(out).toHaveLength(3);
    expect(out[0]).toEqual({ time: 1000, open: 100, high: 110, low: 95, close: 105 });
  });
});

describe("toVolumeBars", () => {
  it("colors bars by candle direction", () => {
    const bars = toVolumeBars(candles);
    expect(bars[0].color).toBe("#26a69a"); // close >= open -> up
    expect(bars[1].color).toBe("#ef5350"); // close < open -> down
    expect(bars.map((b) => b.value)).toEqual([1000, 2000, 1500]);
  });
});

describe("computeStats", () => {
  it("computes window stats from payload only", () => {
    const s = computeStats(candles);
    expect(s.first).toBe(105);
    expect(s.last).toBe(104);
    expect(s.changePct).toBeCloseTo(((104 - 105) / 105) * 100, 5);
    expect(s.high).toBe(110);
    expect(s.low).toBe(95);
    expect(s.volumeTotal).toBe(4500);
  });

  it("returns nulls on empty input (never fabricates)", () => {
    const s = computeStats([]);
    expect(s.last).toBeNull();
    expect(s.changePct).toBeNull();
  });
});

describe("formatters", () => {
  it("formats VND, pct, volume; null -> dash", () => {
    expect(formatVnd(21250)).toBe("21.250");
    expect(formatVnd(null)).toBe("—");
    expect(formatPct(2.5)).toBe("+2.50%");
    expect(formatPct(-1)).toBe("-1.00%");
    expect(formatPct(null)).toBe("—");
    expect(formatVolume(2.5e9)).toContain("tỷ");
    expect(formatVolume(null)).toBe("—");
  });
});

describe("chartModel", () => {
  it("maps a full response", () => {
    const resp: MarketOhlcvResponse = {
      symbol: "HPG",
      candles,
      source_name: "VNDIRECT",
      source_url: "https://dchart.vndirect.com.vn/?symbol=HPG",
      as_of: "2026-11-11T00:00:00+00:00",
      days: 30,
      latency_ms: 12,
    };
    const m = chartModel(resp);
    expect(m.candles).toHaveLength(3);
    expect(m.volumes).toHaveLength(3);
    expect(m.stats.last).toBe(104);
  });
});
