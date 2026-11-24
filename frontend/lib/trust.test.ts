import { describe, expect, it } from "vitest";

import {
  claimIsValid,
  pct,
  stanceColorVar,
  topEntries,
  verdictColorVar,
  viDate,
  volumeBars,
} from "./trust";

describe("trust helpers", () => {
  it("maps verdicts and stances to theme variables only", () => {
    expect(verdictColorVar("supported")).toBe("var(--bull)");
    expect(verdictColorVar("refuted")).toBe("var(--bear)");
    expect(verdictColorVar("contested")).toBe("var(--warn)");
    expect(verdictColorVar("insufficient_evidence")).toBe("var(--muted)");
    expect(stanceColorVar("supports")).toBe("var(--bull)");
    expect(stanceColorVar("refutes")).toBe("var(--bear)");
    expect(stanceColorVar("mentions")).toBe("var(--muted)");
  });

  it("formats percentages defensively", () => {
    expect(pct(0.734)).toBe("73%");
    expect(pct(2)).toBe("100%");
    expect(pct(-1)).toBe("0%");
    expect(pct(Number.NaN)).toBe("—");
    expect(pct(null)).toBe("—");
  });

  it("normalises volume bars", () => {
    expect(volumeBars([])).toEqual([]);
    expect(volumeBars([{ date: "a", count: 0 }])).toEqual([0]);
    expect(
      volumeBars([
        { date: "a", count: 2 },
        { date: "b", count: 4 },
      ])
    ).toEqual([50, 100]);
  });

  it("formats dates in Vietnamese order", () => {
    expect(viDate("2026-11-24T05:00:00+00:00")).toBe("24/11/2026");
    expect(viDate("garbage")).toBe("");
    expect(viDate(null)).toBe("");
  });

  it("sorts coverage entries and drops empties", () => {
    expect(topEntries({ global: 2, VN: 5, x: 0 })).toEqual([
      ["VN", 5],
      ["global", 2],
    ]);
    expect(topEntries(undefined)).toEqual([]);
  });

  it("mirrors the backend claim validation", () => {
    expect(claimIsValid("short")).toBe(false);
    expect(claimIsValid("two words")).toBe(false);
    expect(claimIsValid("Vietnam GDP grew 7% in 2024")).toBe(true);
    expect(claimIsValid("x ".repeat(300))).toBe(false);
  });
});
