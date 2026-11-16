import { describe, expect, it } from "vitest";

import { parseInline } from "./inlineMd";

describe("parseInline", () => {
  it("parses bold and citations like the old renderer", () => {
    expect(parseInline("**ROI cao** xem nguồn [1]")).toEqual([
      { t: "bold", c: [{ t: "text", s: "ROI cao" }] },
      { t: "text", s: " xem nguồn " },
      { t: "cite", n: 1 },
    ]);
  });

  it("parses inline code and shields it from other parsing", () => {
    expect(parseInline("dùng `arr[0]` và **x**")).toEqual([
      { t: "text", s: "dùng " },
      { t: "code", s: "arr[0]" },
      { t: "text", s: " và " },
      { t: "bold", c: [{ t: "text", s: "x" }] },
    ]);
  });

  it("parses italic", () => {
    expect(parseInline("rất *lưu ý* đây")).toEqual([
      { t: "text", s: "rất " },
      { t: "italic", c: [{ t: "text", s: "lưu ý" }] },
      { t: "text", s: " đây" },
    ]);
  });

  it("keeps multiplication asterisks literal (audit round 1)", () => {
    expect(parseInline("2 * 3 * 4")).toEqual([{ t: "text", s: "2 * 3 * 4" }]);
    // Space-flanked asterisks must never become emphasis — the classic
    // "2 * 3 * 4" prose regression.
    expect(parseInline("5 * (a + b) * 2")).toEqual([
      { t: "text", s: "5 * (a + b) * 2" },
    ]);
    // Space-free asterisks follow CommonMark (emphasis inside a word).
    expect(parseInline("3*4*5")).toEqual([
      { t: "text", s: "3" },
      { t: "italic", c: [{ t: "text", s: "4" }] },
      { t: "text", s: "5" },
    ]);
  });

  it("keeps a space-bounded italic opener literal", () => {
    // " * 3 * " — opener followed by space cannot open emphasis.
    expect(parseInline("a * b * c")).toEqual([{ t: "text", s: "a * b * c" }]);
  });

  it("keeps citations live inside bold and italic (nested segments)", () => {
    expect(parseInline("**nguồn [1] chính**")).toEqual([
      {
        t: "bold",
        c: [{ t: "text", s: "nguồn " }, { t: "cite", n: 1 }, { t: "text", s: " chính" }],
      },
    ]);
    expect(parseInline("*x [2] y*")).toEqual([
      {
        t: "italic",
        c: [{ t: "text", s: "x " }, { t: "cite", n: 2 }, { t: "text", s: " y" }],
      },
    ]);
  });

  it("keeps inline code styling inside bold", () => {
    expect(parseInline("**gõ `npm run` xong**")).toEqual([
      {
        t: "bold",
        c: [
          { t: "text", s: "gõ " },
          { t: "code", s: "npm run" },
          { t: "text", s: " xong" },
        ],
      },
    ]);
  });

  it("renders an empty code span as literal backticks (audit round 1)", () => {
    expect(parseInline("a `` b")).toEqual([{ t: "text", s: "a `` b" }]);
  });

  it("parses math segments via mdMath", () => {
    expect(parseInline("ROE $x_i$ cao")).toEqual([
      { t: "text", s: "ROE " },
      { t: "math", s: "x_i" },
      { t: "text", s: " cao" },
    ]);
  });

  it("does not turn money into math", () => {
    expect(parseInline("giá $1,234")).toEqual([{ t: "text", s: "giá $1,234" }]);
  });

  it("keeps an unmatched ** as literal text", () => {
    expect(parseInline("a ** b")).toEqual([{ t: "text", s: "a ** b" }]);
  });

  it("terminates on nested emphasis without runaway recursion", () => {
    const out = parseInline("**a **b** c**");
    expect(out.length).toBeGreaterThan(0);
    expect(out.every((s) => "s" in s || "c" in s || "n" in s)).toBe(true);
  });

  it("caps citation numbers to 3 digits", () => {
    expect(parseInline("[123] ok")).toEqual([
      { t: "cite", n: 123 },
      { t: "text", s: " ok" },
    ]);
    expect(parseInline("[1234] no")).toEqual([{ t: "text", s: "[1234] no" }]);
  });

  it("keeps markdown link syntax literal (renderer has no link support)", () => {
    expect(parseInline("[1](http://x)")).toEqual([
      { t: "cite", n: 1 },
      { t: "text", s: "(http://x)" },
    ]);
  });

  it("returns empty for empty input", () => {
    expect(parseInline("")).toEqual([]);
  });
});
