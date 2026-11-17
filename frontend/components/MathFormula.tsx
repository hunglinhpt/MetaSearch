"use client";

/** KaTeX formula with the chat-demo streaming pattern: keep the LAST
 * successful render while a partial LaTeX run fails to parse mid-stream,
 * falling back to raw text only when nothing has ever parsed.
 *
 * Audit round 1+2 hardening: the last-good fallback only applies when the
 * failing latex is a continuation of the last-good formula (prefix check) —
 * a different formula on the same instance no longer shows stale content;
 * `maxSize` bounds giant-element layout DoS; defense-in-depth length cap. */

import { useEffect, useRef, useState } from "react";

import { MAX_MATH_CHARS } from "../lib/mdMath";

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
  const lastGoodLatexRef = useRef<string | null>(null);

  useEffect(() => {
    let alive = true;
    if (latex.length === 0 || latex.length > MAX_MATH_CHARS) {
      setHtml(null);
      return () => {
        alive = false;
      };
    }
    import("katex")
      .then((katex) => {
        if (!alive) return;
        try {
          const rendered = katex.renderToString(latex, {
            displayMode: display,
            throwOnError: true,
            strict: false,
            trust: false,
            maxSize: 20,
          });
          lastGoodRef.current = rendered;
          lastGoodLatexRef.current = latex;
          setHtml(rendered);
        } catch {
          // Mid-stream partial LaTeX — keep the last complete formula, but
          // only if it is a prefix continuation of THIS one.
          const lastGood = lastGoodRef.current;
          const lastGoodLatex = lastGoodLatexRef.current;
          if (
            lastGood !== null &&
            lastGoodLatex !== null &&
            latex.startsWith(lastGoodLatex)
          ) {
            setHtml(lastGood);
          } else {
            setHtml(null);
          }
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
