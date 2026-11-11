"use client";

/** Shared streaming-markdown renderer (answer + deep research reports):
 * headings, tables, lists, **bold**, and [n] citations as clickable links. */

import type { ReactNode } from "react";

import { safeHref, type Citation } from "../lib/api";

/** Render inline text with **bold** and [n] citation markers as clickable links. */
function renderInlineTokens(
  line: string,
  citationByUrl: Map<number, Citation>
): ReactNode[] {
  const segments = line.split(/(\*\*[^*]+\*\*|\[\d{1,3}\])/g);
  return segments.map((seg, idx) => {
    const citeMatch = seg.match(/^\[(\d{1,3})\]$/);
    if (citeMatch) {
      const n = Number(citeMatch[1]);
      const citation = citationByUrl.get(n);
      const citeHref = citation ? safeHref(citation.url) : undefined;
      return (
        <a
          key={idx}
          className="cite"
          href={citeHref}
          target="_blank"
          rel="noreferrer"
          title={citation ? `${citation.title || citation.url}` : `nguồn [${n}]`}
        >
          [{n}]
        </a>
      );
    }
    if (seg.startsWith("**") && seg.endsWith("**") && seg.length > 4) {
      return <strong key={idx}>{seg.slice(2, -2)}</strong>;
    }
    return <span key={idx}>{seg}</span>;
  });
}

/** Render streaming markdown with headings, tables, lists, bold, and [n] citations. */
export default function AnswerMarkdown({
  text,
  citationByUrl,
}: {
  text: string;
  citationByUrl: Map<number, Citation>;
}) {
  const lines = text.split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    // Table block detection
    if (trimmed.startsWith("|") && trimmed.endsWith("|")) {
      const tableLines: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        tableLines.push(lines[i].trim());
        i++;
      }
      if (tableLines.length >= 2) {
        const parseRow = (r: string) =>
          r
            .slice(1, -1)
            .split("|")
            .map((c) => c.trim());
        const headers = parseRow(tableLines[0]);
        const bodyRows = tableLines
          .slice(1)
          .filter((r) => !/^\|[\s\-:|]+\|$/.test(r))
          .map(parseRow);
        blocks.push(
          <div key={`tbl-${i}`} className="md-table-wrap">
            <table className="md-table">
              <thead>
                <tr>
                  {headers.map((h, hIdx) => (
                    <th key={hIdx}>{renderInlineTokens(h, citationByUrl)}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {bodyRows.map((row, rIdx) => (
                  <tr key={rIdx}>
                    {row.map((cell, cIdx) => (
                      <td key={cIdx}>{renderInlineTokens(cell, citationByUrl)}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
        continue;
      }
    }

    if (trimmed.startsWith("#### ")) {
      blocks.push(
        <h4 key={i}>{renderInlineTokens(trimmed.slice(5), citationByUrl)}</h4>
      );
    } else if (trimmed.startsWith("### ")) {
      blocks.push(
        <h3 key={i}>{renderInlineTokens(trimmed.slice(4), citationByUrl)}</h3>
      );
    } else if (trimmed.startsWith("## ")) {
      blocks.push(
        <h2 key={i}>{renderInlineTokens(trimmed.slice(3), citationByUrl)}</h2>
      );
    } else if (trimmed.startsWith("- ") || trimmed.startsWith("* ")) {
      blocks.push(
        <div key={i} style={{ paddingLeft: "0.8rem", margin: "0.2rem 0" }}>
          • {renderInlineTokens(trimmed.slice(2), citationByUrl)}
        </div>
      );
    } else if (trimmed.length > 0) {
      blocks.push(<p key={i}>{renderInlineTokens(line, citationByUrl)}</p>);
    }
    i++;
  }

  return <div className="answer-body">{blocks}</div>;
}
