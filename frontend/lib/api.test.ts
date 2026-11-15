import { afterEach, describe, expect, it, vi } from "vitest";

import { getLLMHealth, listSkills, safeHref, searchMemoryIndex, validateLLMKeys } from "./api";

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

describe("AI infra client helpers", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("validateLLMKeys posts options to /v1/llm/validate", async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        dry_run: true,
        total_checked: 1,
        configured_count: 1,
        valid_count: 0,
        invalid_count: 0,
        unreachable_count: 0,
        results: [],
      }),
    });
    vi.stubGlobal("fetch", mockFetch);

    const out = await validateLLMKeys({ dry_run: true, providers: ["openrouter"] });
    expect(out.dry_run).toBe(true);
    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, init] = mockFetch.mock.calls[0];
    expect(String(url)).toMatch(/\/v1\/llm\/validate$/);
    expect((init as RequestInit).method).toBe("POST");
  });

  it("getLLMHealth, listSkills, and searchMemoryIndex call expected endpoints", async () => {
    const mockFetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ primary_provider: "groq", ranked_providers: [] }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ enabled: true, total: 9, skills: [] }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ query: "vcb", items: [], storage: "db" }),
      });
    vi.stubGlobal("fetch", mockFetch);

    const health = await getLLMHealth("answer");
    const skills = await listSkills("fpt");
    const mem = await searchMemoryIndex("vcb", { kind: "observation", limit: 5 });

    expect(health.primary_provider).toBe("groq");
    expect(skills.total).toBe(9);
    expect(mem.query).toBe("vcb");
    expect(String(mockFetch.mock.calls[0][0])).toContain("/v1/llm/health?task=answer");
    expect(String(mockFetch.mock.calls[1][0])).toContain("/v1/skills?q=fpt");
    expect(String(mockFetch.mock.calls[2][0])).toContain("/v1/memory/search?q=vcb&limit=5&kind=observation");
  });
});
