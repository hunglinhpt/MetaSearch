/** Math-segment extraction for streaming markdown (ported pattern from
 * deepseek-ai/chat-demo `micromark-extension-llm-math` usage, adapted to the
 * line-based renderer with a conservative currency guard).
 *
 * Financial answers are full of "$1,234" / "US$500" / "$revenue > $cost" — a
 * naive `$...$` span would mangle them into LaTeX. A span is accepted as
 * math ONLY when it carries a STRONG LaTeX marker (a `\command`, `^`, `_`)
 * — weak look-alikes (`=`, `<`, `>`, `{`, `}`) never qualify on their own.
 * Audit round 2: the scanner is index-based and bails out of a piece after
 * 64 failed openers, so a `$`-dense adversarial line stays O(n). */

export type MathSegment = { kind: "display" | "inline"; latex: string };

/** Max chars we accept inside one math span — a run-away stream must not
 * freeze KaTeX (bounded, mirrors the backend's cap philosophy). */
export const MAX_MATH_CHARS = 2000;

/** Max failed openers before the rest of a piece is left as plain text —
 * bounds the rescan cost of `$`-dense adversarial input. */
const MAX_FAILED_OPENERS = 64;

/** True when content carries at least one strong LaTeX marker. Weak
 * look-alikes (=, <, >, {}) deliberately do NOT count: "$revenue > $cost"
 * must stay money, and "$E=mc^2$" still passes via the `^`. */
export function hasMathMarkers(content: string): boolean {
  return /\\[a-zA-Z]+\b|[\^_]/.test(content);
}

/** True when the span content looks like LaTeX, not like money or plain text. */
export function isMathish(content: string): boolean {
  if (content.length === 0 || content.length > MAX_MATH_CHARS) return false;
  if (content.includes("\n")) return false;
  const c = content.charCodeAt(0);
  if (c >= 0x30 && c <= 0x39) return false; // leading digit → "$5 và $10" case
  if (content.startsWith(" ")) return false;
  if (content.endsWith(" ")) return false; // "$revenue > " → comparison prose
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

/** Extract inline math (`$...$`, `\\(...\\)`) from a plain-text piece.
 * Index-based single pass: text runs are sliced once, math becomes a
 * MathSegment; pieces stay in order. */
export function extractInlineMath(text: string): Array<string | MathSegment> {
  const out: Array<string | MathSegment> = [];
  let start = 0; // start of the pending plain-text run
  let i = 0;
  let failures = 0;
  while (i < text.length) {
    const ch = text[i];
    let delim: string | null = null;
    let closeDelim = "";
    if (ch === "$") {
      delim = "$";
      closeDelim = "$";
    } else if (ch === "\\" && text.startsWith("\\(", i)) {
      delim = "\\(";
      closeDelim = "\\)";
    }
    if (delim !== null) {
      const closeAt = findClose(text, i + delim.length, closeDelim);
      const content =
        closeAt === -1 ? null : text.slice(i + delim.length, closeAt);
      if (content !== null && isMathish(content)) {
        if (i > start) out.push(text.slice(start, i));
        out.push({ kind: "inline", latex: content });
        i = closeAt + closeDelim.length;
        start = i;
        continue;
      }
      failures += 1;
      if (failures > MAX_FAILED_OPENERS) {
        break; // adversarial $-dense piece — keep the remainder as text
      }
      i += delim.length; // skip the opener only (findClose may run again)
      continue;
    }
    i += 1;
  }
  if (start < text.length) out.push(text.slice(start));
  return out;
}

/** Extract display math spans (`$$…$$`, `\[…\]`) from one line — used by the
 * block parser's paragraph path so display math inside a prose line follows
 * the same strong-marker + length guards as the fenced `$$` block paths. */
export function extractDisplayMathInline(
  text: string
): Array<string | MathSegment> {
  const out: Array<string | MathSegment> = [];
  let start = 0;
  let i = 0;
  while (i < text.length) {
    let delim: string | null = null;
    let closeDelim = "";
    if (text.startsWith("$$", i)) {
      delim = "$$";
      closeDelim = "$$";
    } else if (text.startsWith("\\[", i)) {
      delim = "\\[";
      closeDelim = "\\]";
    }
    if (delim !== null) {
      const closeAt = findClose(text, i + delim.length, closeDelim);
      const content =
        closeAt === -1 ? null : text.slice(i + delim.length, closeAt).trim();
      if (
        content !== null &&
        content.length > 0 &&
        content.length <= MAX_MATH_CHARS &&
        hasMathMarkers(content)
      ) {
        if (i > start) out.push(text.slice(start, i));
        out.push({ kind: "display", latex: content });
        i = closeAt + closeDelim.length;
        start = i;
        continue;
      }
      i += delim.length;
      continue;
    }
    i += 1;
  }
  if (start < text.length) out.push(text.slice(start));
  return out;
}
