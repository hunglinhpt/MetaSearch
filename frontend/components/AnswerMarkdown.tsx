"use client";

/** Shared streaming-markdown renderer (answer + deep research reports).
 *
 * Upgraded with cherry-picked patterns from deepseek-ai/chat-demo (MIT):
 * fenced code blocks (lazy Prism), KaTeX math (last-good-render fallback),
 * Mermaid diagrams (serial render queue, strict sanitize), inline
 * code/italic/blockquote, and an adaptive render-cost throttle.
 * Citations [n] stay clickable links via the same contract.
 *
 * Audit rounds 1-3 hardening:
 * - the gate now gates the EXPENSIVE part too: parse + element build are
 *   memoized on the throttled snapshot, so parent re-renders (per SSE
 *   token, per keystroke) stop re-running the full document parse;
 * - `gate.accept()` only fires when the shown snapshot actually advanced —
 *   unrelated re-renders no longer re-anchor the pacing window;
 * - blocks carry content-addressed keys (kind + content hash), so a fence
 *   or math block closing mid-stream no longer remounts every downstream
 *   KaTeX/Mermaid component via index churn;
 * - CRLF is normalized once (mermaid and fences choke on \r);
 * - tables cap rows/cells (adversarial-table DoS). */

import { memo, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode, RefObject } from "react";

import { safeHref, type Citation } from "../lib/api";
import { parseInline, type InlineSeg } from "../lib/inlineMd";
import {
  MAX_TABLE_CELLS,
  MAX_TABLE_ROWS,
  parseBlocks,
  type Block,
} from "../lib/markdownBlocks";
import { AdaptiveRenderGate } from "../lib/renderThrottle";
import CodeBlock from "./CodeBlock";
import MathFormula from "./MathFormula";
import MermaidDiagram from "./MermaidDiagram";

/** Adaptive gate for streaming snapshots: accept new text only when the last
 * parse was cheap enough to keep up (cost × 2, 16 ms floor, 2 s cap). The
 * gate is created by AnswerMarkdown and shared here so the measured parse
 * cost feeds back into the pacing decision. */
export function useThrottledStreamText(
  text: string,
  gateRef: RefObject<AdaptiveRenderGate | null>
): string {
  const [shown, setShown] = useState(text);
  const latestRef = useRef(text);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Audit round 1: update the latest snapshot during render too — a timer
  // firing between render and passive effect would otherwise paint
  // one-token-stale text.
  latestRef.current = text;

  useEffect(() => {
    const gate = gateRef.current;
    if (!gate) return;
    if (gate.accepts()) {
      setShown(text);
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      return;
    }
    if (timerRef.current === null) {
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        setShown(latestRef.current);
      }, gate.nextDelayMs());
    }
  }, [text, gateRef]);

  // Unmount hygiene: never leave a timer behind.
  useEffect(
    () => () => {
      if (timerRef.current !== null) clearTimeout(timerRef.current);
    },
    []
  );

  return shown;
}

/** Key strategy per audit rounds 1+3: STATEFUL blocks (fence, math — they
 * own render state: KaTeX last-good, mermaid queue, Prism load) get
 * content-addressed keys so mid-stream restructures don't remount them;
 * cheap text blocks keep occurrence-per-kind keys (stable while the list is
 * append-only, cheap to remount when a tail restructure shifts them). */
function blockKey(block: Block, occurrence: number): string {
  const stateful = block.kind === "fence" || block.kind === "math-display";
  if (!stateful) return `${block.kind}-${occurrence}`;
  const seed =
    block.kind === "fence"
      ? block.code.slice(0, 96) + (block.closed ? "1" : "0")
      : block.latex;
  let hash = 5381;
  for (let i = 0; i < seed.length; i += 1) {
    hash = ((hash << 5) + hash + seed.charCodeAt(i)) | 0;
  }
  return `${block.kind}-${(hash >>> 0).toString(36)}-${occurrence}`;
}

function renderSegments(
  segs: InlineSeg[],
  citationByUrl: Map<number, Citation>,
  keyPrefix: string
): ReactNode[] {
  return segs.map((seg, idx) => {
    const key = `${keyPrefix}-${idx}`;
    switch (seg.t) {
      case "code":
        return <code key={key}>{seg.s}</code>;
      case "bold":
        return (
          <strong key={key}>
            {renderSegments(seg.c, citationByUrl, `${key}b`)}
          </strong>
        );
      case "italic":
        return (
          <em key={key}>
            {renderSegments(seg.c, citationByUrl, `${key}i`)}
          </em>
        );
      case "math":
        return <MathFormula key={key} latex={seg.s} />;
      case "cite": {
        const citation = citationByUrl.get(seg.n);
        const citeHref = citation ? safeHref(citation.url) : undefined;
        return (
          <a
            key={key}
            className="cite"
            href={citeHref}
            target="_blank"
            rel="noreferrer"
            title={citation ? `${citation.title || citation.url}` : `nguồn [${seg.n}]`}
          >
            [{seg.n}]
          </a>
        );
      }
      default:
        return <span key={key}>{seg.s}</span>;
    }
  });
}

function renderInline(
  line: string,
  citationByUrl: Map<number, Citation>,
  keyPrefix: string
): ReactNode[] {
  return renderSegments(parseInline(line), citationByUrl, keyPrefix);
}

function renderBlock(
  block: Block,
  key: string,
  citationByUrl: Map<number, Citation>
): ReactNode {
  switch (block.kind) {
    case "heading": {
      const inner = renderInline(block.text, citationByUrl, `${key}h`);
      if (block.level === 2) return <h2 key={key}>{inner}</h2>;
      if (block.level === 3) return <h3 key={key}>{inner}</h3>;
      return <h4 key={key}>{inner}</h4>;
    }
    case "table": {
      const parseRow = (r: string) =>
        r
          .slice(1, -1)
          .split("|")
          .map((c) => c.trim());
      const headers = parseRow(block.lines[0]);
      const bodyRows = block.lines
        .slice(1)
        .filter((r) => !/^\|[\s\-:|]+\|$/.test(r))
        .map(parseRow)
        .map((row) => row.slice(0, MAX_TABLE_CELLS))
        .slice(0, MAX_TABLE_ROWS);
      return (
        <div key={key} className="md-table-wrap">
          <table className="md-table">
            <thead>
              <tr>
                {headers.map((h, hIdx) => (
                  <th key={hIdx}>
                    {renderInline(h, citationByUrl, `${key}th${hIdx}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {bodyRows.map((row, rIdx) => (
                <tr key={rIdx}>
                  {row.map((cell, cIdx) => (
                    <td key={cIdx}>
                      {renderInline(cell, citationByUrl, `${key}td${rIdx}-${cIdx}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {block.lines.length - 1 > MAX_TABLE_ROWS && (
            <p className="small">
              … bảng được rút gọn còn {MAX_TABLE_ROWS} hàng.
            </p>
          )}
        </div>
      );
    }
    case "fence":
      if (block.lang === "mermaid" && block.closed) {
        return <MermaidDiagram key={key} code={block.code} />;
      }
      return <CodeBlock key={key} code={block.code} lang={block.lang} closed={block.closed} />;
    case "math-display":
      return (
        <div key={key} className="md-math-display">
          <MathFormula latex={block.latex} display />
        </div>
      );
    case "quote":
      return (
        <blockquote key={key} className="md-quote">
          {renderInline(block.text, citationByUrl, `${key}q`)}
        </blockquote>
      );
    case "list-item":
      return (
        <div key={key} style={{ paddingLeft: "0.8rem", margin: "0.2rem 0" }}>
          • {renderInline(block.text, citationByUrl, `${key}li`)}
        </div>
      );
    default:
      return <p key={key}>{renderInline(block.text, citationByUrl, `${key}p`)}</p>;
  }
}

/** Render streaming markdown with headings, tables, lists, fenced code,
 * math, Mermaid, quotes, bold/italic/inline-code, and [n] citations. */
function AnswerMarkdownInner({
  text,
  citationByUrl,
}: {
  text: string;
  citationByUrl: Map<number, Citation>;
}) {
  const gateRef = useRef<AdaptiveRenderGate | null>(null);
  if (gateRef.current === null) gateRef.current = new AdaptiveRenderGate();
  const lastShownRef = useRef<string>("");
  const lastCostRef = useRef(120);
  // Normalize CRLF once — fences and mermaid mis-parse on \r.
  const normalized = useMemo(() => text.replace(/\r\n?/g, "\n"), [text]);
  const shown = useThrottledStreamText(normalized, gateRef);

  // The expensive part is memoized on the throttled snapshot: parent
  // re-renders with unchanged `shown` no longer re-parse the document.
  // The parse cost is measured exactly where it happens.
  const blocks = useMemo(() => {
    const t0 = performance.now();
    const result = parseBlocks(shown);
    lastCostRef.current = performance.now() - t0;
    return result;
  }, [shown]);
  const parsed = useMemo(
    () => {
      const seen = new Map<string, number>();
      return blocks.map((b) => {
        const occ = seen.get(b.kind) ?? 0;
        seen.set(b.kind, occ + 1);
        return renderBlock(b, blockKey(b, occ), citationByUrl);
      });
    },
    [blocks, citationByUrl]
  );

  // The gate paces future snapshots — only when the shown snapshot
  // actually advanced (audit round 1: unrelated re-renders must not
  // re-anchor the pacing window). Executed in useEffect for React StrictMode purity.
  useEffect(() => {
    if (shown !== lastShownRef.current) {
      gateRef.current?.accept(lastCostRef.current);
      lastShownRef.current = shown;
    }
  }, [shown]);

  return <div className="answer-body">{parsed}</div>;
}

/** Memoized: identical props (throttled text unchanged, stable citation map)
 * short-circuit the whole subtree when the parent re-renders for unrelated
 * reasons (status events, keystrokes, engine updates). */
const AnswerMarkdown = memo(AnswerMarkdownInner);
export default AnswerMarkdown;
