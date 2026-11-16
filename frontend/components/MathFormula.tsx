"use client";

/** KaTeX formula with the chat-demo streaming pattern: keep the LAST
 * successful render while a partial LaTeX run fails to parse mid-stream,
 * falling back to raw text only when nothing has ever parsed. */

import { useEffect, useRef, useState } from "react";

import "katex/dist/katex.min.css";

export default function MathFormula({
  latex,
  display = false,
}: {
  latex: string;
  display?: boolean;
}) {
  const [html, setHtml] = useState<string | null>(null);
  const lastGoodRef = useRef<string | null>(null);

  useEffect(() => {
    let alive = true;
    import("katex")
      .then((katex) => {
        if (!alive) return;
        try {
          const rendered = katex.renderToString(latex, {
            displayMode: display,
            throwOnError: true,
            strict: false,
          });
          lastGoodRef.current = rendered;
          setHtml(rendered);
        } catch {
          // Mid-stream partial LaTeX — keep the last complete formula.
          if (lastGoodRef.current !== null) setHtml(lastGoodRef.current);
          else setHtml(null);
        }
      })
      .catch(() => {
        if (alive) setHtml(null);
      });
    return () => {
      alive = false;
    };
  }, [latex, display]);

  if (html === null) {
    return <code className="math-raw">{latex}</code>;
  }
  return (
    <span
      className={display ? "math-display" : "math-inline"}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
