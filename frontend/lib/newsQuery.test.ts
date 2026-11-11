import { describe, expect, it } from "vitest";

import { buildNewsQuery, WHEN_OPERATOR_RE } from "./newsQuery";

const GN = ["google_news"];

describe("buildNewsQuery", () => {
  it("appends when:{n}d for google_news with a plain query", () => {
    expect(buildNewsQuery("tin tức kinh tế", "fallback", 3, GN)).toBe(
      "tin tức kinh tế when:3d",
    );
  });

  it("falls back to the category query when the custom query is blank", () => {
    expect(buildNewsQuery("   ", "tin tài chính", 2, GN)).toBe("tin tài chính when:2d");
  });

  it("never appends a second operator when the query already has one", () => {
    for (const op of ["when:1d", "when:7d", "when:1w", "when:2m", "when:1y", "when:6h", "WHEN:3D"]) {
      expect(buildNewsQuery(`tin ${op}`, "fb", 3, GN)).toBe(`tin ${op}`);
    }
  });

  it("does not treat 'somewhen:3d' as an operator (word boundary)", () => {
    expect(WHEN_OPERATOR_RE.test("somewhen:3d")).toBe(false);
    expect(buildNewsQuery("somewhen:3d", "fb", 3, GN)).toBe("somewhen:3d when:3d");
  });

  it("leaves the query untouched for non-google_news engines", () => {
    expect(buildNewsQuery("hello", "fb", 3, ["searxng"])).toBe("hello");
  });
});
