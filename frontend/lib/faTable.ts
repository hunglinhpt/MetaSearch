/**
 * Pure helpers for the FA compare table (docs/18 B1): metric metadata,
 * value formatting, and the spoken-table builder for TTS. No invented
 * numbers: a missing field renders as "—" and is listed by the backend.
 */

import type { FaSymbolResult } from "./api";

export type MetricDef = {
  key: string;
  label: string;
  format: (v: number) => string;
};

const bil = (v: number) => `${v.toLocaleString("vi-VN", { maximumFractionDigits: 1 })} tỷ`;
const pct = (v: number) => `${v.toFixed(1)}%`;
const vnd = (v: number) => `${Math.round(v).toLocaleString("vi-VN")} đ`;
const num = (v: number) => v.toLocaleString("vi-VN", { maximumFractionDigits: 2 });

export const METRICS: MetricDef[] = [
  { key: "market_cap_vnd_bil", label: "Vốn hóa", format: bil },
  { key: "pe", label: "P/E", format: num },
  { key: "pb", label: "P/B", format: num },
  { key: "eps_vnd", label: "EPS", format: vnd },
  { key: "roe_pct", label: "ROE", format: pct },
  { key: "roa_pct", label: "ROA", format: pct },
  { key: "debt_to_equity", label: "Nợ/Vốn chủ", format: num },
  { key: "revenue_vnd_bil", label: "Doanh thu", format: bil },
  { key: "net_profit_vnd_bil", label: "Lợi nhuận", format: bil },
  { key: "revenue_growth_pct", label: "Tăng DT", format: pct },
  { key: "profit_growth_pct", label: "Tăng LN", format: pct },
  { key: "free_float_pct", label: "Free float", format: pct },
  { key: "dividend_yield_pct", label: "Cổ tức", format: pct },
];

export function formatField(metric: MetricDef, value: number | undefined): string {
  if (value === undefined || !Number.isFinite(value)) return "—";
  return metric.format(value);
}

/** Parse "VCB, FPT; hpg" -> normalized unique uppercase symbols (cap 5). */
export function parseSymbolsInput(raw: string, cap = 5): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const part of raw.split(/[,\s;]+/)) {
    const sym = part.trim().toUpperCase();
    // Charset guard (audit round 9): invalid tokens would 400 at the backend
    // with a raw error — drop them inline instead.
    if (!sym || !/^[A-Z0-9]{1,15}$/.test(sym) || seen.has(sym)) continue;
    seen.add(sym);
    out.push(sym);
    if (out.length >= cap) break;
  }
  return out;
}

/**
 * Build the spoken version of the compare table (read aloud by the TTS
 * engine). Only fields present in the payload are spoken.
 */
export function buildCompareSpeech(results: FaSymbolResult[]): string {
  const lines: string[] = ["Bảng so sánh mã."];
  for (const r of results) {
    const head = r.name ? `${r.symbol}, ${r.name}` : r.symbol;
    const parts: string[] = [];
    for (const m of METRICS) {
      const f = r.fields[m.key];
      if (f) parts.push(`${m.label} ${formatField(m, f.value)}`);
    }
    lines.push(parts.length ? `${head}: ${parts.join(", ")}.` : `${head}: chưa có dữ liệu.`);
  }
  return lines.join(" ");
}
