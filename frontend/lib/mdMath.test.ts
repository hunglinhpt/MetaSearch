import { describe, expect, it } from "vitest";

import {
  extractDisplayMathInline,
  extractInlineMath,
  isMathish,
} from "./mdMath";

describe("isMathish — currency guard", () => {
  it("rejects plain money spans", () => {
    expect(isMathish("1,234")).toBe(false); // "$1,234"
    expect(isMathish("100")).toBe(false); // "$100"
    expect(isMathish(" 5 và 10")).toBe(false);
    expect(isMathish("")).toBe(false);
  });

  it("rejects comparison-operator-only spans (audit round 1)", () => {
    // "$revenue > $cost", "$a = $b", "$x < $y" must stay money/prose.
    expect(isMathish("revenue >")).toBe(false);
    expect(isMathish("a =")).toBe(false);
    expect(isMathish("x <")).toBe(false);
    expect(isMathish("revenue > ")).toBe(false); // trailing space prose
  });

  it("accepts LaTeX-bearing content", () => {
    expect(isMathish("x_i")).toBe(true);
    expect(isMathish("\\frac{a}{b}")).toBe(true);
    expect(isMathish("a^2")).toBe(true);
    expect(isMathish("E=mc^2")).toBe(true); // strong marker (^) carries it
    expect(isMathish("\\sum")).toBe(true);
  });

  it("bounds runaway spans", () => {
    expect(isMathish("x".repeat(2001))).toBe(false);
  });
});

describe("extractInlineMath", () => {
  it("leaves money text untouched", () => {
    expect(extractInlineMath("giá $1,234 và $500")).toEqual([
      "giá $1,234 và $500",
    ]);
  });

  it("leaves currency comparison prose untouched (audit round 1)", () => {
    expect(extractInlineMath("doanh thu $revenue > $cost")).toEqual([
      "doanh thu $revenue > $cost",
    ]);
    expect(extractInlineMath("margin $a = $b theo quý")).toEqual([
      "margin $a = $b theo quý",
    ]);
  });

  it("handles a mixed currency+math line", () => {
    expect(extractInlineMath("ROI $50 và $60 là $x_i$")).toEqual([
      "ROI $50 và $60 là ",
      { kind: "inline", latex: "x_i" },
    ]);
  });

  it("extracts a LaTeX span", () => {
    expect(extractInlineMath("ROE = $\\frac{LN}{EQ}$ trên quý")).toEqual([
      "ROE = ",
      { kind: "inline", latex: "\\frac{LN}{EQ}" },
      " trên quý",
    ]);
  });

  it("extracts \\(...\\) spans", () => {
    expect(extractInlineMath("a \\(x_i\\) b")).toEqual([
      "a ",
      { kind: "inline", latex: "x_i" },
      " b",
    ]);
  });

  it("keeps unterminated spans as text mid-stream", () => {
    expect(extractInlineMath("công thức $\\frac{a}{b")).toEqual([
      "công thức $\\frac{a}{b",
    ]);
  });

  it("respects escaped dollars", () => {
    expect(extractInlineMath("\\$100")).toEqual(["\\$100"]);
  });

  it("survives a $-dense adversarial line without quadratic work", () => {
    const line = Array.from({ length: 500 }, (_, i) => `$${i}`).join(" ");
    const out = extractInlineMath(line);
    // Nothing qualifies (no strong markers) — the whole line stays text.
    expect(out).toEqual([line]);
  });
});

describe("extractDisplayMathInline", () => {
  it("extracts marker-bearing display math and keeps surrounding text", () => {
    expect(extractDisplayMathInline("số $$x^2$$ là")).toEqual([
      "số ",
      { kind: "display", latex: "x^2" },
      " là",
    ]);
  });

  it("treats marker-less display spans as plain text", () => {
    expect(extractDisplayMathInline("số $$x$$ là")).toEqual(["số $$x$$ là"]);
  });

  it("extracts \\[...\\] spans", () => {
    expect(extractDisplayMathInline("a \\[\\frac{a}{b}\\] b")).toEqual([
      "a ",
      { kind: "display", latex: "\\frac{a}{b}" },
      " b",
    ]);
  });

  it("keeps unterminated display math as text", () => {
    expect(extractDisplayMathInline("$$\\frac{a}{b}")).toEqual([
      "$$\\frac{a}{b}",
    ]);
  });

  it("does not treat money as display math", () => {
    expect(extractDisplayMathInline("$$ 100 $$")).toEqual(["$$ 100 $$"]);
  });
});
