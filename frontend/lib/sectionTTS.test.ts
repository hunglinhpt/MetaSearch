import { describe, expect, it } from "vitest";

import { sectionLabel, splitReportSections } from "./sectionTTS";

describe("splitReportSections", () => {
  it("splits on #/##/### headings and keeps order", () => {
    const md = [
      "Đoạn mở đầu không có heading.",
      "",
      "## Tổng quan thị trường",
      "Nội dung tổng quan.",
      "### Biến động giá",
      "VN-Index tăng.",
      "## Kết luận",
      "Mua.",
    ].join("\n");
    const sections = splitReportSections(md);
    expect(sections.map((s) => s.title)).toEqual([
      "Mở đầu",
      "Tổng quan thị trường",
      "Biến động giá",
      "Kết luận",
    ]);
    expect(sections[0].text).toContain("Đoạn mở đầu");
    expect(sections[1].text).toContain("Nội dung tổng quan.");
    expect(sections[2].text).toContain("VN-Index tăng.");
  });

  it("does not treat #### or code fences as section headings", () => {
    const md = "## A\n```md\n# không phải heading\n```\ntext";
    const sections = splitReportSections(md);
    expect(sections).toHaveLength(1);
    expect(sections[0].title).toBe("A");
    expect(sections[0].text).toContain("# không phải heading");
  });

  it("returns one section for a heading-less report", () => {
    const sections = splitReportSections("Chỉ có văn xuôi, không heading.");
    expect(sections).toEqual([{ title: "Báo cáo", text: "Chỉ có văn xuôi, không heading." }]);
  });

  it("returns empty for an empty report", () => {
    expect(splitReportSections("")).toEqual([]);
    expect(splitReportSections("   \n  ")).toEqual([]);
  });

  it("handles CRLF reports", () => {
    const sections = splitReportSections("## A\r\nnội dung A\r\n## B\r\nnội dung B");
    expect(sections.map((s) => s.title)).toEqual(["A", "B"]);
  });

  it("labels sections 1-based for the playlist", () => {
    const [first] = splitReportSections("## Tiêu đề\nthân");
    expect(sectionLabel(first, 0)).toBe("1. Tiêu đề");
  });
});
