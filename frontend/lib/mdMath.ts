/** Math-segment extraction for streaming markdown (ported pattern from
 * deepseek-ai/chat-demo `micromark-extension-llm-math` usage, adapted to the
 * line-based renderer with a conservative currency guard).
 *
 * Financial answers are full of "$1,234" / "US$500" — a naive `$...$` span
 * would mangle them into LaTeX. A `$...$` span is accepted as math ONLY when
 * the content carries at least one LaTeX-ish marker (\, ^, _, {, }, =, <, >)
 * or starts with a LaTeX command (\frac, \sum, ...). */

export type MathSegment = { kind: "display" | "inline"; latex: string };

/** Max chars we accept inside one math span — a run-away stream must not
 * freeze KaTeX (bounded, mirrors the backend's cap philosophy). */
export const MAX_MATH_CHARS = 2000;

/** True when the span content carries at least one LaTeX-ish marker
 * (\cmd, ^, _, {, }, =, <, >). Shared by inline and display guards. */
export function hasMathMarkers(content: string): boolean {
  return /[\\^_{}=<>\u2212]|\\[a-zA-Z]+\b/.test(content);
}

/** True when the span content looks like LaTeX, not like money or plain text. */
export function isMathish(content: string): boolean {
  if (content.length === 0 || content.length > MAX_MATH_CHARS) return false;
  if (content.includes("\n")) return false;
  const c = content.charCodeAt(0);
  if (c >= 0x30 && c <= 0x39) return false; // leading digit → "$5 và $10" case
  if (content.startsWith(" ")) return false;
  return hasMathMarkers(content);
}

/** Find the closing delimiter of a span starting at `from`. A backslash is
 * an escape only when it does NOT start the close delimiter itself — `\)`
 * must close `\(…` instead of being skipped as an escape. Returns -1 when
 * unterminated. */
function findClose(line: string, from: number, delim: string): number {
  let i = from;
  while (i < line.length) {
    if (line[i] === "\\" && !line.startsWith(delim, i)) {
      i += 2;
      continue;
    }
    if (line.startsWith(delim, i)) return i;
    i += 1;
  }
  return -1;
}

/** Merge consecutive string pieces so callers get clean text segments. */
function mergeStrings(parts: Array<string | MathSegment>): Array<string | MathSegment> {
  const out: Array<string | MathSegment> = [];
  for (const part of parts) {
    if (
      typeof part === "string" &&
      out.length > 0 &&
      typeof out[out.length - 1] === "string"
    ) {
      out[out.length - 1] = out[out.length - 1] + part;
    } else {
      out.push(part);
    }
  }
  return out;
}

/** Extract display math from one line: `$$...$$` and `\[..., ...\]`.
 * Returns the leftover plain-text pieces and the math segments in order. */
export function extractDisplayMath(
  line: string
): Array<string | MathSegment> {
  const out: Array<string | MathSegment> = [];
  let rest = line;
  // Loop: each pass peels one display-math span off `rest`.
  for (;;) {
    const dd = rest.indexOf("$$");
    const bd = rest.indexOf("\\[");
    if (dd === -1 && bd === -1) break;
    let openAt: number;
    let delim: string;
    let closeDelim: string;
    if (dd !== -1 && (bd === -1 || dd < bd)) {
      openAt = dd;
      delim = "$$";
      closeDelim = "$$";
    } else {
      openAt = bd;
      delim = "\\[";
      closeDelim = "\\]";
    }
    const closeAt = findClose(rest, openAt + delim.length, closeDelim);
    if (closeAt === -1) break; // unterminated → stream still coming
    const latex = rest.slice(openAt + delim.length, closeAt);
    if (latex.trim().length > 0 && hasMathMarkers(latex) && latex.length <= MAX_MATH_CHARS) {
      if (openAt > 0) out.push(rest.slice(0, openAt));
      out.push({ kind: "display", latex: latex.trim() });
      rest = rest.slice(closeAt + closeDelim.length);
    } else {
      // Looks like money ($$ 100 $$) or junk → leave untouched, stop here.
      break;
    }
  }
  if (rest.length > 0) out.push(rest);
  return mergeStrings(out);
}

/** Extract inline math (`$...$`, `\\(...\\)`) from a plain-text piece.
 * Text pieces are returned unchanged; math becomes a MathSegment. */
export function extractInlineMath(text: string): Array<string | MathSegment> {
  const out: Array<string | MathSegment> = [];
  let rest = text;
  let carry = ""; // prefix held while scanning past non-math openers
  for (;;) {
    // Pick the earliest opener of either flavor.
    let openAt = -1;
    let delim = "";
    let closeDelim = "";
    const firstDollar = rest.indexOf("$");
    const firstParen = rest.indexOf("\\(");
    if (firstDollar !== -1 && (firstParen === -1 || firstDollar < firstParen)) {
      openAt = firstDollar;
      delim = "$";
      closeDelim = "$";
    } else if (firstParen !== -1) {
      openAt = firstParen;
      delim = "\\(";
      closeDelim = "\\)";
    }
    if (openAt === -1) break;
    const closeAt = findClose(rest, openAt + delim.length, closeDelim);
    if (closeAt === -1) break; // unterminated → keep as text
    const content = rest.slice(openAt + delim.length, closeAt);
    if (isMathish(content)) {
      carry += rest.slice(0, openAt);
      if (carry.length > 0) out.push(carry);
      carry = "";
      out.push({ kind: "inline", latex: content });
      rest = rest.slice(closeAt + closeDelim.length);
    } else {
      // Not math (e.g. "$100") → hold the opener in carry, scan past it.
      carry += rest.slice(0, openAt + delim.length);
      rest = rest.slice(openAt + delim.length);
    }
  }
  carry += rest;
  if (carry.length > 0) out.push(carry);
  return mergeStrings(out);
}
