import { describe, expect, it } from "vitest";

import { parseBlocks } from "./markdownBlocks";

describe("parseBlocks", () => {
  it("keeps heading/table/list behavior unchanged", () => {
    const blocks = parseBlocks("## Tiêu đề\n\n- mục một\n\n| a | b |\n|---|---|\n| 1 | 2 |");
    expect(blocks).toEqual([
      { kind: "heading", level: 2, text: "Tiêu đề" },
      { kind: "list-item", text: "mục một" },
      { kind: "table", lines: ["| a | b |", "|---|---|", "| 1 | 2 |"] },
    ]);
  });

  it("parses a closed code fence with language", () => {
    const blocks = parseBlocks("```python\nprint(1)\nprint(2)\n```");
    expect(blocks).toEqual([
      { kind: "fence", lang: "python", code: "print(1)\nprint(2)", closed: true },
    ]);
  });

  it("renders an unclosed fence as a partial code block mid-stream", () => {
    const blocks = parseBlocks("```js\nconst x = 1;\n");
    expect(blocks).toEqual([
      { kind: "fence", lang: "js", code: "const x = 1;", closed: false },
    ]);
  });

  it("marks mermaid fences by language", () => {
    const blocks = parseBlocks("```mermaid\ngraph TD; A-->B;\n```");
    expect(blocks).toEqual([
      { kind: "fence", lang: "mermaid", code: "graph TD; A-->B;", closed: true },
    ]);
  });

  it("parses blockquote groups", () => {
    expect(parseBlocks("> dòng 1\n> dòng 2\nsau quote")).toEqual([
      { kind: "quote", text: "dòng 1\ndòng 2" },
      { kind: "paragraph", text: "sau quote" },
    ]);
  });

  it("parses a $$ math block across lines", () => {
    expect(parseBlocks("$$\n\\frac{a}{b}\n$$")).toEqual([
      { kind: "math-display", latex: "\\frac{a}{b}" },
    ]);
  });

  it("keeps an unterminated $$ block as paragraph text mid-stream", () => {
    expect(parseBlocks("$$\n\\frac{a}{b}")).toEqual([
      { kind: "paragraph", text: "$$" },
      { kind: "paragraph", text: "\\frac{a}{b}" },
    ]);
  });

  it("parses single-line display math", () => {
    expect(parseBlocks("$$E=mc^2$$")).toEqual([
      { kind: "math-display", latex: "E=mc^2" },
    ]);
  });

  it("does not treat a table row missing the trailing pipe as a table", () => {
    const blocks = parseBlocks("| chỉ mở đầu");
    expect(blocks).toEqual([{ kind: "paragraph", text: "| chỉ mở đầu" }]);
  });

  it("handles a mixed document end to end", () => {
    const doc = [
      "## Kết quả",
      "",
      "ROE tăng $x_i$ **mạnh** [1].",
      "",
      "```mermaid",
      "graph TD; A-->B;",
      "```",
      "",
      "> Miễn trừ",
      "",
      "```python",
      "print(1)",
      "```",
    ].join("\n");
    const kinds = parseBlocks(doc).map((b) => b.kind);
    expect(kinds).toEqual([
      "heading",
      "paragraph",
      "fence",
      "quote",
      "fence",
    ]);
  });
});
