"use client";

/** Fenced code block with copy button + lazy Prism highlighting
 * (cherry-picked from deepseek-ai/chat-demo CodeBlock; plain-text fallback
 * keeps the block readable when the language is unsupported or SSR).
 *
 * Audit round 3: while a fence is still OPEN (streaming), its code grows on
 * every accepted snapshot — highlighting would re-run Prism per snapshot
 * outside the throttle's cost measurement, so open fences render as plain
 * text and highlight only once the closing fence arrives. */

import { useEffect, useRef, useState } from "react";

import { highlightCode } from "../lib/codeHighlight";

export default function CodeBlock({
  code,
  lang,
  closed = true,
}: {
  code: string;
  lang: string;
  closed?: boolean;
}) {
  const [html, setHtml] = useState("");
  const [copied, setCopied] = useState(false);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!closed) return; // plain text while streaming — no per-snapshot Prism
    let alive = true;
    highlightCode(code, lang).then((h) => {
      if (alive) setHtml(h);
    });
    return () => {
      alive = false;
    };
  }, [code, lang, closed]);

  // Unmount hygiene for the copied-label timer (audit round 1).
  useEffect(
    () => () => {
      if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current);
    },
    []
  );

  const copy = async (): Promise<void> => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current);
      copyTimerRef.current = setTimeout(() => {
        copyTimerRef.current = null;
        setCopied(false);
      }, 1500);
    } catch {
      // clipboard unavailable (permissions / non-secure context) — ignore
    }
  };

  return (
    <div className="md-code">
      <div className="md-code-bar">
        <span className="md-code-lang">{lang || "code"}</span>
        <button type="button" className="md-code-copy" onClick={copy}>
          {copied ? "✓ Đã copy" : "Copy"}
        </button>
      </div>
      {html ? (
        <pre>
          <code dangerouslySetInnerHTML={{ __html: html }} />
        </pre>
      ) : (
        <pre>
          <code>{code}</code>
        </pre>
      )}
    </div>
  );
}
