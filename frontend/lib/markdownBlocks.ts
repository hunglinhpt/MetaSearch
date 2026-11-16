/** Block-level markdown parser (pure, node-testable) — upgrades the line
 * renderer with fenced code/mermaid blocks, display math, and blockquotes
 * while preserving the existing heading/table/list behavior. Fences that are
 * still open during streaming render as plain code blocks (the chat-demo
 * streaming-fallback, expressed as data instead of a re-render timer).
 *
 * Audit round 1+2 hardening: the lone-table rewind (a single `|…|` line no
 * longer swallows the line after it), MAX_MATH_CHARS on ALL display-math
 * paths, MAX_FENCE_CHARS truncation, and display-math extraction wired into
 * paragraphs (no more dead-code drift between parser and renderer). */

import { extractDisplayMathInline, hasMathMarkers, MAX_MATH_CHARS } from "./mdMath";

/** Cap for one fenced code block — beyond this the tail is truncated with a
 * notice (plain-text fallback path is otherwise unbounded). */
export const MAX_FENCE_CHARS = 200_000;

/** Cap for table rows/cells rendered per table (adversarial-table DoS). */
export const MAX_TABLE_ROWS = 500;
export const MAX_TABLE_CELLS = 20;

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
      let code = body.join("\n").replace(/\n$/, "");
      if (code.length > MAX_FENCE_CHARS) {
        const cut = code.length - MAX_FENCE_CHARS;
        code = code.slice(0, MAX_FENCE_CHARS) + `\n… [đã cắt ${cut} ký tự]`;
      }
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
      if (
        closed &&
        latex.length > 0 &&
        latex.length <= MAX_MATH_CHARS &&
        hasMathMarkers(latex)
      ) {
        blocks.push({ kind: "math-display", latex });
      } else {
        // Unterminated mid-stream, money, or oversized → plain text fallback.
        blocks.push({ kind: "paragraph", text: line });
        for (const b of body) blocks.push({ kind: "paragraph", text: b });
        if (closed) blocks.push({ kind: "paragraph", text: "$$" });
      }
      continue;
    }

    // Single-line display math: $$...$$ (both delimiters on one line) —
    // same length cap + marker guard as the multi-line path (audit round 2).
    if (trimmed.startsWith("$$") && trimmed.endsWith("$$") && trimmed.length > 4) {
      const latex = trimmed.slice(2, -2).trim();
      if (
        latex.length > 0 &&
        latex.length <= MAX_MATH_CHARS &&
        hasMathMarkers(latex)
      ) {
        blocks.push({ kind: "math-display", latex });
        i += 1;
        continue;
      }
    }

    // Table block (unchanged behavior).
    if (trimmed.startsWith("|") && trimmed.endsWith("|")) {
      const tableStart = i;
      const tableLines: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        tableLines.push(lines[i].trim());
        i += 1;
      }
      if (tableLines.length >= 2) {
        blocks.push({ kind: "table", lines: tableLines });
        continue;
      }
      // Audit round 1: a lone `|…|` line used to swallow the NEXT line —
      // rewind so the normal path renders both.
      i = tableStart;
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
      // Inline display math within a paragraph (e.g. "số $$x^2$$ là") —
      // split into prose + display-math blocks; otherwise the whole line
      // stays one paragraph (leading whitespace preserved).
      const pieces = extractDisplayMathInline(line);
      if (pieces.some((p) => typeof p !== "string")) {
        for (const piece of pieces) {
          if (typeof piece === "string") {
            if (piece.trim().length > 0) {
              blocks.push({ kind: "paragraph", text: piece });
            }
          } else {
            blocks.push({ kind: "math-display", latex: piece.latex });
          }
        }
      } else {
        blocks.push({ kind: "paragraph", text: line });
      }
    }
    i += 1;
  }

  return blocks;
}
