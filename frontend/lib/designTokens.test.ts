/**
 * DESIGN.md <-> globals.css parity (docs/34 §4, stitch-skills integration).
 * DESIGN.md is the spec AI design agents (Google Stitch & co.) read; this test
 * keeps it truthful: same tokens, same values, both themes, and the "system"
 * dark block mirrors the explicit dark block. Also guards the "no colour
 * literals in components" rule.
 */
import { readdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";

import { describe, expect, it } from "vitest";

const ROOT = resolve(process.cwd());
const css = readFileSync(join(ROOT, "app", "globals.css"), "utf-8");
const design = readFileSync(join(ROOT, "DESIGN.md"), "utf-8");

function tokens(body: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const m of body.matchAll(/(--[\w-]+):\s*([^;]+);/g)) out[m[1]] = m[2].trim();
  return out;
}

function block(start: RegExp): Record<string, string> {
  const m = css.match(start);
  if (!m || m.index === undefined) throw new Error(`block not found: ${start}`);
  const from = css.indexOf("{", m.index) + 1;
  const to = css.indexOf("}", from);
  return tokens(css.slice(from, to));
}

const light = block(/\n:root\s*\{/);
const dark = block(/:root\[data-theme="dark"\]\s*\{/);
const systemDark = block(/:root:not\(\[data-theme\]\)\s*\{/);

const table: Record<string, { light: string; dark: string }> = {};
for (const m of design.matchAll(/^\| `(--[\w-]+)` \| `([^`]+)` \| `([^`]+)` \|/gm)) {
  table[m[1]] = { light: m[2], dark: m[3] };
}

describe("DESIGN.md token table", () => {
  it("lists every globals.css token with its exact light and dark value", () => {
    expect(Object.keys(table).sort()).toEqual(Object.keys(light).sort());
    for (const [k, v] of Object.entries(table)) {
      expect(v.light, `${k} light`).toBe(light[k]);
      expect(v.dark, `${k} dark`).toBe(dark[k]);
    }
  });

  it("defines every token in both themes", () => {
    expect(Object.keys(dark).sort()).toEqual(Object.keys(light).sort());
  });

  it("system-dark block mirrors the explicit dark block", () => {
    expect(systemDark).toEqual(dark);
  });
});

describe("no colour literals in components", () => {
  it("TSX components use theme variables only", () => {
    const dir = join(ROOT, "components");
    const offenders: string[] = [];
    for (const f of readdirSync(dir).filter((n) => n.endsWith(".tsx"))) {
      // Canvas libraries cannot read CSS variables: themeVar("--x", "#fallback")
      // resolves the variable at draw time; its SSR fallback literal is allowed.
      const src = readFileSync(join(dir, f), "utf-8").replace(
        /themeVar\("--[\w-]+",\s*"#[0-9a-fA-F]{3,6}"\)/g,
        "themeVar()"
      );
      // Hex colours inside style props / strings (#abc, #aabbcc) — not ids or anchors.
      for (const m of src.matchAll(/["'`]\s*#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?\b/g)) {
        offenders.push(`${f}: ${m[0]}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});
