/** Block-level markdown parser (pure, node-testable) — upgrades the line
 * renderer with fenced code/mermaid blocks, display math, and blockquotes
 * while preserving the existing heading/table/list behavior. Fences that are
 * still open during streaming render as plain code blocks (the chat-demo
 * streaming-fallback, expressed as data instead of a re-render timer). */

import { hasMathMarkers, MAX_MATH_CHARS } from "./mdMath";

export type Block =
  | { kind: "heading"; level: 2 | 3 | 4; text: string }
  | { kind: "paragraph"; text: string }
  | { kind: "list-item"; text: string }
  | { kind: "table"; lines: string[] }
  | { kind: "fence"; lang: string; code: string; closed: boolean }
  | { kind: "math-display"; latex: string }
  | { kind: "quote"; text: string };

/** Parse a (possibly still-streaming) markdown document into blocks. */
export function parseBlocks(text: string): Block[] {
  const lines = text.split("\n");
  const blocks: Block[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    // Fenced code / mermaid: ``` lang
    if (trimmed.startsWith("```")) {
      const lang = trimmed.slice(3).trim().toLowerCase();
      const body: string[] = [];
      i += 1;
      let closed = false;
      while (i < lines.length) {
        if (lines[i].trim().startsWith("```")) {
          closed = true;
          i += 1;
          break;
        }
        body.push(lines[i]);
        i += 1;
      }
      // Drop one artifact trailing newline from split("\n") — copy stays clean.
      const code = body.join("\n").replace(/\n$/, "");
      blocks.push({ kind: "fence", lang, code, closed });
      continue;
    }

    // Display math: $$ on its own line opens a LaTeX block until $$.
    // Guarded by hasMathMarkers so "$$ 100 $$" prose stays text.
    if (trimmed === "$$") {
      const body: string[] = [];
      i += 1;
      let closed = false;
      while (i < lines.length) {
        if (lines[i].trim() === "$$") {
          closed = true;
          i += 1;
          break;
        }
        body.push(lines[i]);
        i += 1;
      }
      const latex = body.join("\n").trim();
      if (closed && latex.length > 0 && latex.length <= MAX_MATH_CHARS && hasMathMarkers(latex)) {
        blocks.push({ kind: "math-display", latex });
      } else {
        // Unterminated mid-stream, money, or oversized → plain text fallback.
        blocks.push({ kind: "paragraph", text: line });
        for (const b of body) blocks.push({ kind: "paragraph", text: b });
        if (closed) blocks.push({ kind: "paragraph", text: "$$" });
      }
      continue;
    }

    // Single-line display math: $$...$$ (both delimiters on one line).
    if (trimmed.startsWith("$$") && trimmed.endsWith("$$") && trimmed.length > 4) {
      const latex = trimmed.slice(2, -2).trim();
      if (latex.length > 0 && hasMathMarkers(latex)) {
        blocks.push({ kind: "math-display", latex });
        i += 1;
        continue;
      }
    }

    // Table block (unchanged behavior).
    if (trimmed.startsWith("|") && trimmed.endsWith("|")) {
      const tableLines: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        tableLines.push(lines[i].trim());
        i += 1;
      }
      if (tableLines.length >= 2) {
        blocks.push({ kind: "table", lines: tableLines });
        continue;
      }
    }

    // Blockquote: consecutive > lines collapse into one block.
    if (trimmed.startsWith(">")) {
      const quoted: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) {
        quoted.push(lines[i].trim().replace(/^>\s?/, ""));
        i += 1;
      }
      blocks.push({ kind: "quote", text: quoted.join("\n") });
      continue;
    }

    if (trimmed.startsWith("#### ")) {
      blocks.push({ kind: "heading", level: 4, text: trimmed.slice(5) });
    } else if (trimmed.startsWith("### ")) {
      blocks.push({ kind: "heading", level: 3, text: trimmed.slice(4) });
    } else if (trimmed.startsWith("## ")) {
      blocks.push({ kind: "heading", level: 2, text: trimmed.slice(3) });
    } else if (trimmed.startsWith("- ") || trimmed.startsWith("* ")) {
      blocks.push({ kind: "list-item", text: trimmed.slice(2) });
    } else if (trimmed.length > 0) {
      blocks.push({ kind: "paragraph", text: line });
    }
    i += 1;
  }

  return blocks;
}
