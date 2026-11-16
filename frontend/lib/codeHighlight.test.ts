import { describe, expect, it } from "vitest";

import { highlightCode } from "./codeHighlight";

describe("highlightCode", () => {
  it("returns empty on the server (SSR guard)", async () => {
    // vitest runs in node — window is undefined — the fail-open path.
    expect(await highlightCode("const x = 1;", "typescript")).toBe("");
  });

  it("returns empty for empty code", async () => {
    expect(await highlightCode("", "python")).toBe("");
  });
});
