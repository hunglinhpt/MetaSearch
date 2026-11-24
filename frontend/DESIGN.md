# DESIGN.md — MetaSearch Web UI design system

> Stitch-compatible design spec (format of `google-labs-code/stitch-skills`
> `design-md` / `extract-design-md`). It is the single source of truth an AI
> design agent (Google Stitch, Claude, Cursor…) must follow when generating or
> editing screens for this app. **The token table below is enforced by
> `lib/designTokens.test.ts`**: it must match `app/globals.css` byte-for-byte,
> so this file can never silently drift from the shipped UI. Evaluation and
> integration decision: [docs/34 §4](../docs/34-trust-insight-layer-v4.5.md).

## 1. Visual theme & atmosphere

Calm, information-dense research console — "Bloomberg terminal meets GitHub".
Neutral grey canvas, white/charcoal cards, ONE blue accent, semantic
green/red/amber reserved for meaning (up/down, supported/refuted, caution) —
never decoration. Vietnamese-first copy, sentence case, emoji as tab glyphs.
Three themes: `light`, `dark`, `system` (default; follows the OS live).

## 2. Color palette & roles

Rules:

1. Components use **only** `var(--token)`; no hex/rgb literals in TSX or CSS
   outside the theme blocks of `globals.css`.
2. Every token exists in both themes; the `@media (prefers-color-scheme: dark)`
   "system" block mirrors the explicit dark block exactly (test-enforced).
3. Semantic colours carry meaning: `--bull` = positive/supported,
   `--bear` = negative/refuted, `--warn` = contested/caution, `--muted` =
   unknown/insufficient. Never swap them for aesthetics.

| Token | Light | Dark | Role |
|---|---|---|---|
| `--bg` | `#f6f8fa` | `#0b0f14` | Page background (canvas) |
| `--fg` | `#1f2328` | `#e6edf3` | Primary text |
| `--muted` | `#57606a` | `#9aa7b4` | Secondary text, metadata, captions |
| `--card` | `#ffffff` | `#131a22` | Surface of cards/panels |
| `--border` | `#d0d7de` | `#223040` | Hairline borders, dividers |
| `--accent` | `#0969da` | `#4c9aff` | Primary action, links, focus, timeline rail |
| `--accent-fg` | `#ffffff` | `#04121f` | Text on accent fills |
| `--input-bg` | `#ffffff` | `#0d131a` | Inputs and selects |
| `--code-bg` | `#eef2f6` | `#0d131a` | Code blocks |
| `--raise` | `rgba(31, 35, 40, 0.03)` | `rgba(255, 255, 255, 0.02)` | Subtle raised surface |
| `--raise-2` | `rgba(31, 35, 40, 0.05)` | `rgba(255, 255, 255, 0.04)` | Raised surface level 2 |
| `--hover-raise` | `rgba(31, 35, 40, 0.06)` | `rgba(255, 255, 255, 0.05)` | Hover wash |
| `--hover-raise-strong` | `rgba(31, 35, 40, 0.1)` | `rgba(255, 255, 255, 0.1)` | Strong hover wash |
| `--tint` | `rgba(9, 105, 218, 0.09)` | `rgba(76, 154, 255, 0.1)` | Accent tint (chips, verdict banner) |
| `--tint-soft` | `rgba(9, 105, 218, 0.055)` | `rgba(76, 154, 255, 0.06)` | Soft accent tint |
| `--tint-strong` | `rgba(9, 105, 218, 0.14)` | `rgba(76, 154, 255, 0.16)` | Strong accent tint |
| `--bull` | `#1a7f37` | `#34d399` | Positive / supported / price up |
| `--bear` | `#cf222e` | `#f87171` | Negative / refuted / price down |
| `--warn` | `#9a6700` | `#fbbf24` | Caution / contested |
| `--err` | `#cf222e` | `#ff8484` | Error messages |
| `--c-blue` | `#0969da` | `#4c9aff` | Badge hue blue |
| `--c-teal` | `#0f766e` | `#2dd4bf` | Badge hue teal |
| `--c-green` | `#1a7f37` | `#34d399` | Badge hue green |
| `--c-amber` | `#9a6700` | `#fbbf24` | Badge hue amber |
| `--c-red` | `#cf222e` | `#f87171` | Badge hue red |
| `--c-violet` | `#8250df` | `#a78bfa` | Badge hue violet |
| `--syn-comment` | `#6e7781` | `#7f8c98` | Syntax: comment |
| `--syn-keyword` | `#cf222e` | `#ff7b72` | Syntax: keyword |
| `--syn-string` | `#0a3069` | `#a5d6ff` | Syntax: string |
| `--syn-function` | `#8250df` | `#d2a8ff` | Syntax: function |
| `--syn-number` | `#0550ae` | `#79c0ff` | Syntax: number |
| `--syn-op` | `#24292f` | `#c9d1d9` | Syntax: operator |

## 3. Typography

- Family: system UI stack — `ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`
  (Vietnamese diacritics render natively; no web-font download).
- Scale (rem): body 1 · card title 1.15 · secondary 0.82–0.88 · meta/badges 0.75–0.78.
- Numbers in tables/timelines use `font-variant-numeric: tabular-nums`.
- Weights: 400 body, 600 titles/active chips, 700 verdicts/stance badges.

## 4. Components

- **Card** (`.card`): `--card` surface, 1px `--border`, radius 12px; the unit of every panel.
- **Search row** (`.search-row`): text input (radius 10px, `--input-bg`) + primary submit
  (`--accent` fill, `--accent-fg` text); disabled = reduced opacity, never hidden.
- **Chip** (`.chip`, pill radius 999px, `--tint` fill) — filters, coverage counts;
  `.chip-toggle.on` = selected (accent fill).
- **Nav tab** (`.nav-tab`): emoji + Vietnamese label; `.active` underline in `--accent`.
- **Verdict banner** (`.verdict-banner`): 4px left rule in the verdict's semantic colour,
  `--tint` fill, title 700.
- **Evidence quote** (`.evidence-quote`): verbatim source text, 2px `--border` left rule —
  quotes are never paraphrased in the UI.
- **Timeline** (`.timeline-item`): 2px `--accent` rail + 10px dot, date column
  tabular-nums; stacks vertically under 600px.
- **Briefing block** (`.briefing-block`), **warn box** (`.warn-box`): tinted call-outs via
  `color-mix(in srgb, var(--token) N%, transparent)`.

## 5. Layout principles

- Single column, max-width container, generous 0.75–1rem rhythm; cards stack.
- Mobile first: every row wraps (`flex-wrap`), long URLs break (`overflow-wrap: anywhere`).
- Every external link goes through `safeHref()` and opens with `rel="noopener noreferrer"`.
- Accessibility: interactive groups carry `role`/`aria-*` (tabs `aria-selected`,
  toggles `aria-pressed`), inputs have `aria-label`, colour is never the only signal
  (verdicts also have an icon + text).

## 6. Do / Don't for generated screens

- DO cite sources inline and show their tier + timestamp; DON'T invent numbers or dates.
- DO show degraded states honestly ("chưa đủ bằng chứng", "một phần nguồn lỗi").
- DON'T add new colour literals, web fonts, or third-party CDNs (CSP and theme tests fail).
