/**
 * Split a Deep Research markdown report into speakable sections (docs/18 §A2).
 * Headings `#`..`###` open a new section; text before the first heading is the
 * "Mở đầu" section — unless the whole report has no headings at all, then the
 * single section is titled "Báo cáo". Code fences are never treated as
 * headings. Pure functions — vitest-covered (no recognizer needed).
 */

export type ReportSection = { title: string; text: string };

export function splitReportSections(md: string): ReportSection[] {
  // Two-pass: the preamble title depends on whether ANY heading exists in the
  // whole document ("Mở đầu" only when headings come later; a fully
  // heading-less report is one plain "Báo cáo" section).
  const hasAnyHeading = /^#{1,3}\s+/m.test(md) && !isInsideFenceOnly(md);
  const preambleTitle = hasAnyHeading ? "Mở đầu" : "Báo cáo";
  const lines = md.split(/\r?\n/);
  const sections: ReportSection[] = [];
  let current: ReportSection | null = null;
  let inFence = false;

  const flush = () => {
    if (current && current.text.trim()) {
      // Trim the trailing newline artifact — keep interior formatting intact.
      sections.push({ ...current, text: current.text.replace(/\s+$/, "") });
    }
  };

  for (const line of lines) {
    if (/^\s*```/.test(line)) {
      inFence = !inFence;
      if (!current) current = { title: preambleTitle, text: "" };
      current.text += line + "\n";
      continue;
    }
    if (!inFence) {
      const heading = line.match(/^(#{1,3})\s+(.*)$/);
      if (heading) {
        flush();
        current = { title: heading[2].trim(), text: "" };
        continue;
      }
    }
    if (!current) current = { title: preambleTitle, text: "" };
    current.text += line + "\n";
  }
  flush();

  if (sections.length) return sections;
  return md.trim() ? [{ title: "Báo cáo", text: md }] : [];
}

/** A doc whose only `#` lines live inside code fences has no real headings. */
function isInsideFenceOnly(md: string): boolean {
  let inFence = false;
  for (const line of md.split(/\r?\n/)) {
    if (/^\s*```/.test(line)) {
      inFence = !inFence;
      continue;
    }
    if (!inFence && /^#{1,3}\s+/.test(line)) return false;
  }
  return true;
}

/** Human label for the playlist ("1. Mở đầu", "2. Tổng quan..."). */
export function sectionLabel(section: ReportSection, index: number): string {
  return `${index + 1}. ${section.title}`;
}
