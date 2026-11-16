import { describe, expect, it } from "vitest";

import { parseInline } from "./inlineMd";

describe("parseInline", () => {
  it("parses bold and citations like the old renderer", () => {
    expect(parseInline("**ROI cao** xem nguồn [1]")).toEqual([
      { t: "bold", s: "ROI cao" },
      { t: "text", s: " xem nguồn " },
      { t: "cite", n: 1 },
    ]);
  });

  it("parses inline code and shields it from other parsing", () => {
    expect(parseInline("dùng `arr[0]` và **x**")).toEqual([
      { t: "text", s: "dùng " },
      { t: "code", s: "arr[0]" },
      { t: "text", s: " và " },
      { t: "bold", s: "x" },
    ]);
  });

  it("parses italic", () => {
    expect(parseInline("rất *lưu ý* đây")).toEqual([
      { t: "text", s: "rất " },
      { t: "italic", s: "lưu ý" },
      { t: "text", s: " đây" },
    ]);
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

  it("caps citation numbers to 3 digits", () => {
    expect(parseInline("[123] ok")).toEqual([
      { t: "cite", n: 123 },
      { t: "text", s: " ok" },
    ]);
    expect(parseInline("[1234] no")).toEqual([{ t: "text", s: "[1234] no" }]);
  });

  it("returns empty for empty input", () => {
    expect(parseInline("")).toEqual([]);
  });
});
