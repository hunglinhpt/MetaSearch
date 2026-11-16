import { describe, expect, it } from "vitest";

import {
  extractDisplayMath,
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

  it("accepts LaTeX-bearing content", () => {
    expect(isMathish("x_i")).toBe(true);
    expect(isMathish("\\frac{a}{b}")).toBe(true);
    expect(isMathish("a^2")).toBe(true);
    expect(isMathish("E=mc^2")).toBe(true);
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

  it("extracts a LaTeX span", () => {
    const out = extractInlineMath("ROE = $\\frac{LN}{EQ}$ trên quý");
    expect(out).toEqual([
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
});

describe("extractDisplayMath", () => {
  it("extracts $$..$$ on one line", () => {
    expect(extractDisplayMath("$$\\frac{a}{b}$$")).toEqual([
      { kind: "display", latex: "\\frac{a}{b}" },
    ]);
  });

  it("extracts marker-bearing display math and keeps surrounding text", () => {
    expect(extractDisplayMath("số $$x^2$$ là")).toEqual([
      "số ",
      { kind: "display", latex: "x^2" },
      " là",
    ]);
  });

  it("treats marker-less display spans as plain text", () => {
    expect(extractDisplayMath("số $$x$$ là")).toEqual(["số $$x$$ là"]);
  });

  it("keeps unterminated display math as text", () => {
    expect(extractDisplayMath("$$\\frac{a}{b}")).toEqual(["$$\\frac{a}{b}"]);
  });

  it("does not treat money as display math", () => {
    expect(extractDisplayMath("$$ 100 $$")).toEqual(["$$ 100 $$"]);
  });
});
