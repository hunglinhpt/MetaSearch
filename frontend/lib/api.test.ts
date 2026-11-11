import { describe, expect, it } from "vitest";

import { safeHref } from "./api";

describe("safeHref", () => {
  it("allows http and https (any case)", () => {
    expect(safeHref("https://vnexpress.net/a")).toBe("https://vnexpress.net/a");
    expect(safeHref("http://example.com")).toBe("http://example.com");
    expect(safeHref("HTTPS://EXAMPLE.COM/x")).toBe("HTTPS://EXAMPLE.COM/x");
  });

  it("blocks javascript:, data: and every non-http scheme", () => {
    expect(safeHref("javascript:alert(1)")).toBeUndefined();
    expect(safeHref("data:text/html,<script>alert(1)</script>")).toBeUndefined();
    expect(safeHref("file:///etc/passwd")).toBeUndefined();
    expect(safeHref("vbscript:x")).toBeUndefined();
  });

  it("fails closed on leading whitespace/control chars and homoglyphs", () => {
    expect(safeHref("  https://ok.example")).toBeUndefined();
    expect(safeHref("\thttps://ok.example")).toBeUndefined();
    expect(safeHref("ｈttps://fullwidth.example")).toBeUndefined();
    expect(safeHref("https:\\evil.example")).toBeUndefined();
  });

  it("keeps the scheme pinned even with userinfo", () => {
    // userinfo cannot escalate the scheme; the https prefix wins
    expect(safeHref("https://user@example.com")).toBe("https://user@example.com");
  });
});
