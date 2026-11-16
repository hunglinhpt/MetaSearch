/** Lazy syntax-highlighting via Prism (cherry-picked pattern from
 * deepseek-ai/chat-demo `services/markdown/highlight.ts`): per-language
 * dynamic imports so the bundle only pays for languages actually rendered,
 * with Prism's component dependency ordering (cpp needs c, tsx needs jsx+ts,
 * php needs markup-templating) and alias mapping.
 *
 * SSR/node guard: returns "" (caller falls back to plain text) — Prism's
 * component files touch `window`, so loading happens client-side only. */

const ALIASES: Record<string, string> = {
  py: "python",
  js: "javascript",
  ts: "typescript",
  sh: "bash",
  shell: "bash",
  zsh: "bash",
  yml: "yaml",
  golang: "go",
  md: "markdown",
};

/** Explicit loaders — an import(`prismjs/components/prism-${lang}`) template
 * would make the bundler sweep all 597 language files into one lazy chunk. */
const LOADERS: Record<string, () => Promise<unknown>> = {
  clike: () => import("prismjs/components/prism-clike"),
  markup: () => import("prismjs/components/prism-markup"),
  c: () => import("prismjs/components/prism-c"),
  cpp: () => import("prismjs/components/prism-cpp"),
  java: () => import("prismjs/components/prism-java"),
  javascript: () => import("prismjs/components/prism-javascript"),
  json: () => import("prismjs/components/prism-json"),
  typescript: () => import("prismjs/components/prism-typescript"),
  jsx: () => import("prismjs/components/prism-jsx"),
  tsx: () => import("prismjs/components/prism-tsx"),
  python: () => import("prismjs/components/prism-python"),
  bash: () => import("prismjs/components/prism-bash"),
  css: () => import("prismjs/components/prism-css"),
  yaml: () => import("prismjs/components/prism-yaml"),
  sql: () => import("prismjs/components/prism-sql"),
  go: () => import("prismjs/components/prism-go"),
  rust: () => import("prismjs/components/prism-rust"),
  markdown: () => import("prismjs/components/prism-markdown"),
};

/** Prism component load order (language definitions that reuse others). */
const DEPS: Record<string, string[]> = {
  c: ["clike"],
  cpp: ["c", "clike"],
  java: ["clike"],
  javascript: ["clike"],
  json: [],
  typescript: ["javascript", "clike"],
  jsx: ["javascript", "clike", "markup"],
  tsx: ["typescript", "javascript", "clike", "jsx", "markup"],
  python: [],
  bash: [],
  css: [],
  yaml: [],
  sql: [],
  go: ["clike"],
  rust: ["clike"],
  markdown: ["markup"],
};

/** Bounded allow-list — answers only ever carry common code fences. */
const SUPPORTED = new Set(Object.keys(DEPS));

let corePromise: Promise<typeof import("prismjs")> | null = null;
const loadedLangs = new Set<string>();

async function loadCore(): Promise<typeof import("prismjs")> {
  if (!corePromise) {
    corePromise = import("prismjs");
  }
  return corePromise;
}

async function loadLanguage(lang: string): Promise<boolean> {
  const loader = LOADERS[lang];
  if (!loader) return false;
  for (const dep of DEPS[lang] ?? []) {
    if (!(await loadLanguage(dep))) return false;
  }
  try {
    await loader();
    loadedLangs.add(lang);
    return true;
  } catch {
    return false;
  }
}

/** Highlight `code` as `lang`; returns "" when unsupported or server-side
 * (caller then renders plain text). Fail-open, never throws. */
export async function highlightCode(
  code: string,
  langRaw: string
): Promise<string> {
  if (typeof window === "undefined") return "";
  const lang = ALIASES[langRaw] ?? langRaw;
  if (!SUPPORTED.has(lang) || code.length === 0 || code.length > 100_000) {
    return "";
  }
  try {
    const Prism = await loadCore();
    if (!loadedLangs.has(lang) && !(await loadLanguage(lang))) return "";
    if (!Prism.languages[lang]) return "";
    return Prism.highlight(code, Prism.languages[lang], lang);
  } catch {
    return "";
  }
}
