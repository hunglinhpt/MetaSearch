import { describe, expect, it } from "vitest";

import type { FaSymbolResult } from "./api";
import { buildCompareSpeech, formatField, METRICS, parseSymbolsInput } from "./faTable";

describe("parseSymbolsInput", () => {
  it("normalizes, dedupes, caps at 5", () => {
    expect(parseSymbolsInput("vcb, FPT; hpg HPG")).toEqual(["VCB", "FPT", "HPG"]);
    expect(
      parseSymbolsInput("AAA BBB CCC DDD EEE FFF GGG")
    ).toEqual(["AAA", "BBB", "CCC", "DDD", "EEE"]);
    expect(parseSymbolsInput("   ")).toEqual([]);
  });
});

describe("formatField", () => {
  const pe = METRICS.find((m) => m.key === "pe")!;
  it("formats numbers and renders missing as dash", () => {
    expect(formatField(pe, 9.5)).toBe("9,5");
    expect(formatField(pe, undefined)).toBe("—");
    expect(formatField(pe, NaN)).toBe("—");
  });
});

describe("buildCompareSpeech", () => {
  it("speaks only fields present in the payload", () => {
    const r: FaSymbolResult = {
      symbol: "VCB",
      name: " Vietcombank",
      exchange: "HSX",
      fields: {
        pe: { value: 9.5, source: "tcbs", as_of: "2026-09-30", validated: true },
      },
      missing_fields: ["pb"],
      sources_used: ["tcbs"],
      source_urls: [],
      as_of: null,
      published_at: null,
    };
    const speech = buildCompareSpeech([r]);
    expect(speech).toContain("VCB");
    expect(speech).toContain("P/E 9,5");
    expect(speech).not.toContain("P/B"); // missing -> not spoken
    const empty: FaSymbolResult = { ...r, symbol: "XXX", name: null, fields: {} };
    expect(buildCompareSpeech([empty])).toContain("chưa có dữ liệu");
  });
});
