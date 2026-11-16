"use client";

/** Shared streaming-markdown renderer (answer + deep research reports).
 *
 * Upgraded with cherry-picked patterns from deepseek-ai/chat-demo (MIT):
 * fenced code blocks (lazy Prism), KaTeX math (last-good-render fallback),
 * Mermaid diagrams (serial render queue, strict sandbox), inline
 * code/italic/blockquote, and an adaptive render-cost throttle so token
 * storms on long reports repaint at a bounded rate instead of O(n²).
 * Citations [n] stay clickable links via the same contract. */

import { useEffect, useRef, useState } from "react";
import type { ReactNode, RefObject } from "react";

import { safeHref, type Citation } from "../lib/api";
import { parseInline, type InlineSeg } from "../lib/inlineMd";
import { parseBlocks, type Block } from "../lib/markdownBlocks";
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

  useEffect(() => {
    latestRef.current = text;
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
        return <strong key={key}>{seg.s}</strong>;
      case "italic":
        return <em key={key}>{seg.s}</em>;
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
  idx: number,
  citationByUrl: Map<number, Citation>
): ReactNode {
  switch (block.kind) {
    case "heading": {
      const inner = renderInline(block.text, citationByUrl, `h${idx}`);
      if (block.level === 2) return <h2 key={idx}>{inner}</h2>;
      if (block.level === 3) return <h3 key={idx}>{inner}</h3>;
      return <h4 key={idx}>{inner}</h4>;
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
        .map(parseRow);
      return (
        <div key={idx} className="md-table-wrap">
          <table className="md-table">
            <thead>
              <tr>
                {headers.map((h, hIdx) => (
                  <th key={hIdx}>{renderInline(h, citationByUrl, `th${idx}-${hIdx}`)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {bodyRows.map((row, rIdx) => (
                <tr key={rIdx}>
                  {row.map((cell, cIdx) => (
                    <td key={cIdx}>
                      {renderInline(cell, citationByUrl, `td${idx}-${rIdx}-${cIdx}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }
    case "fence":
      if (block.lang === "mermaid" && block.closed) {
        return <MermaidDiagram key={idx} code={block.code} />;
      }
      return <CodeBlock key={idx} code={block.code} lang={block.lang} />;
    case "math-display":
      return (
        <div key={idx} className="md-math-display">
          <MathFormula latex={block.latex} display />
        </div>
      );
    case "quote":
      return (
        <blockquote key={idx} className="md-quote">
          {renderInline(block.text, citationByUrl, `q${idx}`)}
        </blockquote>
      );
    case "list-item":
      return (
        <div key={idx} style={{ paddingLeft: "0.8rem", margin: "0.2rem 0" }}>
          • {renderInline(block.text, citationByUrl, `li${idx}`)}
        </div>
      );
    default:
      return <p key={idx}>{renderInline(block.text, citationByUrl, `p${idx}`)}</p>;
  }
}

/** Render streaming markdown with headings, tables, lists, fenced code,
 * math, Mermaid, quotes, bold/italic/inline-code, and [n] citations. */
export default function AnswerMarkdown({
  text,
  citationByUrl,
}: {
  text: string;
  citationByUrl: Map<number, Citation>;
}) {
  const gateRef = useRef<AdaptiveRenderGate | null>(null);
  if (gateRef.current === null) gateRef.current = new AdaptiveRenderGate();
  const shown = useThrottledStreamText(text, gateRef);

  // Measure this parse; the gate uses the cost to pace future snapshots.
  const t0 = performance.now();
  const blocks = parseBlocks(shown);
  const parsed = blocks.map((b, bIdx) => renderBlock(b, bIdx, citationByUrl));
  const cost = performance.now() - t0;
  // Mutating a ref during render mirrors chat-demo's shipped pattern; the
  // value is only consumed by the throttle effect after commit.
  gateRef.current.accept(cost);

  return <div className="answer-body">{parsed}</div>;
}
