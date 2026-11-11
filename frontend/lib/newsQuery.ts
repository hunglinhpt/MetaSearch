/**
 * Pure query builder for the news tab (docs/16-web-ui-news-tts.md §1).
 *
 * `google_news` ignores the server-side `freshness_days` param, so the client
 * appends Google News RSS's `when:{n}d` operator to the query — the window the
 * engine actually enforces. Kept as a pure function so vitest can pin the
 * edge cases (existing operator, substring false-positives, unit letters).
 */

/** Matches a real `when:` operator (word boundary, units h/d/w/m/y). */
export const WHEN_OPERATOR_RE = /\bwhen:\d+[dhwmy]/i;

export function buildNewsQuery(
  rawQuery: string,
  fallbackQuery: string,
  freshnessDays: number,
  engines: string[],
): string {
  const base = rawQuery.trim() || fallbackQuery;
  if (engines.includes("google_news") && !WHEN_OPERATOR_RE.test(base)) {
    return `${base} when:${freshnessDays}d`;
  }
  return base;
}
