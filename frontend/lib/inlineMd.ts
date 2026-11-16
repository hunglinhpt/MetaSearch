/** Inline-markdown segment parser (pure, node-testable) — upgrades the
 * line-based renderer with inline code, italic, and math while preserving
 * the existing **bold** and [n] citation behavior (deepseek-ai/chat-demo
 * cherry-pick; citation contract unchanged). */

import { extractInlineMath } from "./mdMath";

export type InlineSeg =
  | { t: "text"; s: string }
  | { t: "code"; s: string }
  | { t: "bold"; s: string }
  | { t: "italic"; s: string }
  | { t: "cite"; n: number }
  | { t: "math"; s: string };

/** Earliest index of any of the markers in `s` from `from` (inclusive). */
function earliestOf(s: string, markers: string[], from: number): number {
  let best = -1;
  for (const m of markers) {
    const at = s.indexOf(m, from);
    if (at !== -1 && (best === -1 || at < best)) best = at;
  }
  return best;
}

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

/** Parse one line into typed inline segments. Depth caps bold-inside-bold
 * pathology on malformed input. */
export function parseInline(line: string, depth = 0): InlineSeg[] {
  if (line.length === 0) return [];
  const out: InlineSeg[] = [];
  let plain = "";
  let i = 0;

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
        flushPlain();
        if (close > i + 1) out.push({ t: "code", s: line.slice(i + 1, close) });
        i = close + 1;
        continue;
      }
    }
    if (ch === "*" && line.startsWith("**", i)) {
      const close = findSpanClose(line, i + 2, "**");
      if (close !== -1) {
        flushPlain();
        out.push({ t: "bold", s: collapse(line.slice(i + 2, close)) });
        i = close + 2;
        continue;
      }
    }
    if (ch === "*") {
      const close = findSpanClose(line, i + 1, "*");
      if (close !== -1 && close > i + 1) {
        flushPlain();
        out.push({ t: "italic", s: collapse(line.slice(i + 1, close)) });
        i = close + 1;
        continue;
      }
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

/** Flatten nested segments inside bold/italic to plain text (bold inner
 * keeps its text; nested markup markers are dropped, matching the old
 * renderer's tolerance of malformed input). */
function collapse(s: string): string {
  return parseInline(s, 1)
    .map((seg) => (seg.t === "cite" ? `[${seg.n}]` : seg.s))
    .join("");
}
