"use client";

/** Fenced code block with copy button + lazy Prism highlighting
 * (cherry-picked from deepseek-ai/chat-demo CodeBlock; plain-text fallback
 * keeps the block readable when the language is unsupported or SSR). */

import { useEffect, useState } from "react";

import { highlightCode } from "../lib/codeHighlight";

export default function CodeBlock({ code, lang }: { code: string; lang: string }) {
  const [html, setHtml] = useState("");
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let alive = true;
    highlightCode(code, lang).then((h) => {
      if (alive) setHtml(h);
    });
    return () => {
      alive = false;
    };
  }, [code, lang]);

  const copy = async (): Promise<void> => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
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
