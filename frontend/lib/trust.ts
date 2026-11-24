/**
 * Pure helpers for the Trust & Insight tab (docs/34): verdict/stance labels,
 * colour tokens (CSS variables only — follows DESIGN.md / theme system) and
 * the daily-volume bar normalisation. No DOM, no fetch: unit-tested in
 * trust.test.ts.
 */

import type { VerifyStance, VerifyVerdict } from "./api";

export const VERDICT_LABEL: Record<VerifyVerdict, string> = {
  supported: "Có bằng chứng ủng hộ",
  refuted: "Có bằng chứng bác bỏ",
  contested: "Bằng chứng trái chiều",
  insufficient_evidence: "Chưa đủ bằng chứng",
};

export const VERDICT_ICON: Record<VerifyVerdict, string> = {
  supported: "✅",
  refuted: "❌",
  contested: "⚖️",
  insufficient_evidence: "❔",
};

/** CSS custom property for a verdict (never a hard-coded colour). */
export function verdictColorVar(v: VerifyVerdict): string {
  switch (v) {
    case "supported":
      return "var(--bull)";
    case "refuted":
      return "var(--bear)";
    case "contested":
      return "var(--warn)";
    default:
      return "var(--muted)";
  }
}

export const STANCE_LABEL: Record<VerifyStance, string> = {
  supports: "Ủng hộ",
  refutes: "Bác bỏ",
  mentions: "Chỉ nhắc tới",
};

export function stanceColorVar(s: VerifyStance): string {
  return s === "supports" ? "var(--bull)" : s === "refutes" ? "var(--bear)" : "var(--muted)";
}

/** 0..1 -> "73%" (clamped, NaN-safe). */
export function pct(x: number | null | undefined): string {
  if (typeof x !== "number" || !Number.isFinite(x)) return "—";
  return `${Math.round(Math.max(0, Math.min(1, x)) * 100)}%`;
}

/** Bar heights (0..100) for a volume histogram; empty/zero input -> zeros. */
export function volumeBars(rows: { date: string; count: number }[]): number[] {
  const max = rows.reduce((m, r) => Math.max(m, Number.isFinite(r.count) ? r.count : 0), 0);
  if (max <= 0) return rows.map(() => 0);
  return rows.map((r) => Math.round(((Number.isFinite(r.count) ? r.count : 0) / max) * 100));
}

/** ISO timestamp -> "dd/mm/yyyy" (Vietnamese order); invalid -> "". */
export function viDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const dd = String(d.getUTCDate()).padStart(2, "0");
  const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
  return `${dd}/${mm}/${d.getUTCFullYear()}`;
}

/** Sorted [key, count] pairs, largest first, for coverage chips. */
export function topEntries(rec: Record<string, number> | undefined, n = 8): [string, number][] {
  return Object.entries(rec ?? {})
    .filter(([, v]) => Number.isFinite(v) && v > 0)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, n);
}

/** Validate a claim the same way the backend does (8..500 chars, >= 3 words). */
export function claimIsValid(raw: string): boolean {
  const text = raw.trim().replace(/\s+/g, " ");
  return text.length >= 8 && text.length <= 500 && text.split(" ").length >= 3;
}
