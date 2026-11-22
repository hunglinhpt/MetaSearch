import type { Metadata } from "next";
import type { ReactNode } from "react";

import "./globals.css";

export const metadata: Metadata = {
  title: "MetaSearch",
  description: "AI meta-search engine: multi-engine retrieval, clean extraction, cited answers.",
};

/**
 * Pre-paint theme bootstrap: reads the stored choice (dark / light / system)
 * and stamps <html data-theme> before first paint so the page never flashes
 * the wrong palette. "system" is resolved purely in CSS via
 * prefers-color-scheme, so no JS is needed to follow OS changes afterwards.
 * Kept in sync with lib/theme.ts (THEME_STORAGE_KEY / valid values) and styled
 * without CSS variables: it must run before vars exist.
 */
const THEME_BOOTSTRAP = `(function(){try{var t=localStorage.getItem("metasearch.theme");if(t!=="dark"&&t!=="light"&&t!=="system"){t="system";}document.documentElement.dataset.theme=t;}catch(e){document.documentElement.dataset.theme="system";}})();`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="vi" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOTSTRAP }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
