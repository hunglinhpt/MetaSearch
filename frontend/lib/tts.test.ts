import { describe, expect, it } from "vitest";

import { chunkForSpeech, stripForSpeech } from "./tts";

describe("stripForSpeech", () => {
  it("removes citation markers [n]", () => {
    expect(stripForSpeech("VN-Index tăng 1,2% [1][12].")).toBe("VN-Index tăng 1,2% .");
  });

  it("keeps the link label only, never the URL", () => {
    expect(stripForSpeech("xem [SSI Research](https://www.ssi.com.vn/r) tại đây")).toBe(
      "xem SSI Research tại đây",
    );
  });

  it("link rule runs before citation rule: [12](url) leaves no orphan paren", () => {
    expect(stripForSpeech("theo [12](https://a.b) nguồn")).toBe("theo 12 nguồn");
  });

  it("strips bare URLs entirely", () => {
    expect(stripForSpeech("Nguồn: https://vnexpress.net/x?utm=1 xem tiếp.")).toBe(
      "Nguồn: xem tiếp.",
    );
  });

  it("tolerates one level of nested parens in link URLs", () => {
    expect(stripForSpeech("[Hà Nội](https://vi.wikipedia.org/wiki/Hà_Nội_(thủ_đô))")).toBe(
      "Hà Nội",
    );
  });

  it("drops fenced code, images, headings, emphasis, table separators", () => {
    const md = [
      "## Tiêu đề",
      "```js",
      "const x = 1;",
      "```",
      "![ảnh](https://a.b/img.png)",
      "**đậm** và `code` và ~~gạch~~",
      "| A | B |",
      "|---|---|",
      "| 1 | 2 |",
    ].join("\n");
    const out = stripForSpeech(md);
    expect(out).not.toContain("const x");
    expect(out).not.toContain("https://");
    expect(out).not.toContain("---");
    expect(out).not.toContain("**");
    expect(out).toContain("Tiêu đề");
    expect(out).toContain("đậm");
    expect(out).toContain("A B");
  });

  it("unescapes common entities and strips zero-width chars and numbered bullets", () => {
    expect(stripForSpeech("Tom &amp; Jerry&nbsp;1. Mục một")).toBe("Tom & Jerry 1. Mục một");
    expect(stripForSpeech("a\u200Bb")).toBe("ab");
    // numbered list marker is stripped, content kept
    expect(stripForSpeech("1. Tin một\n2. Tin hai")).toBe("Tin một Tin hai");
  });

  it("returns empty for markup-only input", () => {
    expect(stripForSpeech("![x](https://a.b)")).toBe("");
  });
});

describe("chunkForSpeech", () => {
  it("packs sentences under maxLen", () => {
    const chunks = chunkForSpeech("Một. Hai. Ba.", 10);
    expect(chunks.length).toBeGreaterThan(1);
    expect(chunks.join(" ")).toBe("Một. Hai. Ba.");
  });

  it("hard-splits a single spaceless word longer than maxLen", () => {
    const word = "a".repeat(500);
    const chunks = chunkForSpeech(word, 220);
    expect(chunks.length).toBe(3);
    expect(chunks.every((c) => c.length <= 220)).toBe(true);
    expect(chunks.join("")).toBe(word);
  });

  it("clamps a non-positive maxLen instead of looping forever", () => {
    const chunks = chunkForSpeech("abc def", 0);
    expect(chunks.length).toBeGreaterThan(0);
    expect(chunks.every((c) => c.length <= 1)).toBe(true);
  });

  it("does not split inside Vietnamese abbreviations like TP.HCM", () => {
    const chunks = chunkForSpeech("Thị trường TP.HCM tăng. Kết thúc.", 220);
    expect(chunks).toEqual(["Thị trường TP.HCM tăng. Kết thúc."]);
  });
});
