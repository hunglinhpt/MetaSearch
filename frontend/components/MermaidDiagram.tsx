"use client";

/** Mermaid diagram with the chat-demo robustness patterns: a module-level
 * serial render queue (mermaid's renderer is not re-entrant), strict
 * security level, a bounded input, and a graceful fallback to the raw code
 * block when the diagram fails to parse. The streaming fallback lives in
 * the block parser: an unclosed ```mermaid fence renders as plain code
 * until the closing fence arrives. */

import { useEffect, useState } from "react";

/** Serialized so concurrent diagrams (answer + research report) never race. */
let chain: Promise<unknown> = Promise.resolve();
let initialized = false;

const MAX_MERMAID_CHARS = 20_000;

async function renderSvg(id: string, code: string): Promise<string | null> {
  const run = async (): Promise<string | null> => {
    try {
      const mermaid = (await import("mermaid")).default;
      if (!initialized) {
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          theme: "dark",
        });
        initialized = true;
      }
      const { svg } = await mermaid.render(id, code);
      return svg;
    } catch {
      return null;
    }
  };
  const result = chain.then(run, run);
  chain = result.catch(() => undefined);
  return result;
}

export default function MermaidDiagram({ code }: { code: string }) {
  const [svg, setSvg] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (code.length === 0 || code.length > MAX_MERMAID_CHARS) {
      setFailed(true);
      return;
    }
    let alive = true;
    const id = `mmd-${Math.random().toString(36).slice(2, 10)}`;
    renderSvg(id, code).then((out) => {
      if (!alive) return;
      if (out) setSvg(out);
      else setFailed(true);
    });
    return () => {
      alive = false;
    };
  }, [code]);

  if (svg) {
    return (
      <div
        className="md-mermaid"
        // mermaid strict mode runs DOMPurify over the SVG (scripts and
        // event handlers stripped). strict is a sanitize pass, NOT a
        // sandboxed iframe — foreignObject/<style> survive by design, so
        // this stays defense-in-depth behind the parser caps, not the
        // only line of defense (audit round 2).
        dangerouslySetInnerHTML={{ __html: svg }}
      />
    );
  }
  if (failed) {
    return (
      <div className="md-mermaid md-mermaid-fallback">
        <pre>
          <code>{code}</code>
        </pre>
      </div>
    );
  }
  return (
    <div className="md-mermaid md-mermaid-pending">
      <span className="spinner" /> Đang vẽ diagram…
    </div>
  );
}
