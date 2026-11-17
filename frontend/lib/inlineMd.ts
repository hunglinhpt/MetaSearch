/** Inline-markdown segment parser (pure, node-testable) — upgrades the
 * line-based renderer with inline code, italic, and math while preserving
 * the existing **bold** and [n] citation behavior (deepseek-ai/chat-demo
 * cherry-pick; citation contract unchanged).
 *
 * Audit round 1 hardening: italic requires a left-flanking opener and
 * non-space-bounded content (so "2 * 3 * 4" stays literal multiplication),
 * bold/italic keep NESTED segments (citations/inline code inside emphasis
 * stay live — the old renderer's split preserved citations inside bold),
 * and depth is enforced to bound nested emphasis on malformed input. */

import { extractInlineMath } from "./mdMath";

export type InlineSeg =
  | { t: "text"; s: string }
  | { t: "code"; s: string }
  | { t: "bold"; c: InlineSeg[] }
  | { t: "italic"; c: InlineSeg[] }
  | { t: "cite"; n: number }
  | { t: "math"; s: string };

/** Max emphasis nesting depth — bounds recursion on malformed input. */
const MAX_EMPHASIS_DEPTH = 2;
/** Cap failed emphasis scans per line to prevent O(n^2) on pathological `*` runs. */
const MAX_FAILED_EMPHASIS = 64;

function findSpanClose(s: string, from: number, delim: string): number {
  let i = from;
  while (i < s.length) {
    if (s[i] === "\\") {
      i += 2;
      continue;
    }
    if (s.startsWith(delim, i)) return i;
    i += 1;
  }
  return -1;
}

const CITE_RE = /^\[(\d{1,3})\]/;

/** Parse one line into typed inline segments. */
export function parseInline(line: string, depth = 0): InlineSeg[] {
  if (line.length === 0) return [];
  // Depth cap: deeper than MAX_EMPHASIS_DEPTH, everything is literal text.
  if (depth > MAX_EMPHASIS_DEPTH) return [{ t: "text", s: line }];
  const out: InlineSeg[] = [];
  let plain = "";
  let i = 0;
  let failedEmphasis = 0;

  const flushPlain = (): void => {
    if (plain.length === 0) return;
    for (const piece of extractInlineMath(plain)) {
      if (typeof piece === "string") out.push({ t: "text", s: piece });
      else out.push({ t: "math", s: piece.latex });
    }
    plain = "";
  };

  while (i < line.length) {
    const ch = line[i];
    if (ch === "`") {
      const close = line.indexOf("`", i + 1);
      if (close !== -1) {
        if (close === i + 1) {
          // Empty code span (``) → both backticks stay literal text.
          plain += "``";
          i = close + 1;
          continue;
        }
        flushPlain();
        out.push({ t: "code", s: line.slice(i + 1, close) });
        i = close + 1;
        continue;
      }
    }
    if (ch === "*" && line.startsWith("**", i) && failedEmphasis < MAX_FAILED_EMPHASIS) {
      const close = findSpanClose(line, i + 2, "**");
      if (close !== -1) {
        const inner = line.slice(i + 2, close);
        if (inner.length > 0) {
          flushPlain();
          out.push({ t: "bold", c: parseInline(inner, depth + 1) });
          i = close + 2;
          continue;
        }
      }
      failedEmphasis += 1;
    }
    if (
      ch === "*" &&
      failedEmphasis < MAX_FAILED_EMPHASIS &&
      i + 1 < line.length &&
      !/\s/.test(line[i + 1])
    ) {
      const close = findSpanClose(line, i + 1, "*");
      // CommonMark left-flanking rule: no whitespace right after the
      // opener, none right before the closer — "2 * 3 * 4" stays literal.
      const inner = close === -1 ? "" : line.slice(i + 1, close);
      if (
        close !== -1 &&
        inner.length > 0 &&
        !/^\s/.test(inner) &&
        !/\s$/.test(inner)
      ) {
        flushPlain();
        out.push({ t: "italic", c: parseInline(inner, depth + 1) });
        i = close + 1;
        continue;
      }
      failedEmphasis += 1;
    }
    if (ch === "[") {
      const m = CITE_RE.exec(line.slice(i, i + 6));
      if (m) {
        flushPlain();
        out.push({ t: "cite", n: Number(m[1]) });
        i += m[0].length;
        continue;
      }
    }
    plain += ch;
    i += 1;
  }
  flushPlain();
  return out;
}
