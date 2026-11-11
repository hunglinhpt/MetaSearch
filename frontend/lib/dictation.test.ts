import { describe, expect, it } from "vitest";

import { dictationErrorReason, mergeTranscripts } from "./dictation";

describe("dictationErrorReason", () => {
  it("maps browser error codes to actionable reasons", () => {
    expect(dictationErrorReason("not-allowed")).toBe("permission");
    expect(dictationErrorReason("service-not-allowed")).toBe("permission");
    expect(dictationErrorReason("no-speech")).toBe("no-speech");
    expect(dictationErrorReason("network")).toBe("network");
    expect(dictationErrorReason("audio-capture")).toBe("no-microphone");
  });

  it("falls back to the raw code for unknown errors", () => {
    expect(dictationErrorReason("weird-code")).toBe("weird-code");
    expect(dictationErrorReason("")).toBe("unknown");
  });
});

describe("mergeTranscripts", () => {
  it("joins committed chunks with a space", () => {
    expect(mergeTranscripts("pháp y BCTC", "của VCB", null)).toBe("pháp y BCTC của VCB");
  });

  it("appends the live interim tail after committed text", () => {
    expect(mergeTranscripts("pháp y", "BCTC", "của ng")).toBe("pháp y BCTC của ng");
  });

  it("returns interim alone when nothing is committed yet", () => {
    expect(mergeTranscripts("", null, "lãi su")).toBe("lãi su");
  });

  it("trims whitespace on every part", () => {
    expect(mergeTranscripts("  pháp y  ", " BCTC ", "  của  ")).toBe("pháp y BCTC của");
  });

  it("ignores empty new-final chunks", () => {
    expect(mergeTranscripts("pháp y", "", "BCTC")).toBe("pháp y BCTC");
    expect(mergeTranscripts("pháp y", null, null)).toBe("pháp y");
  });

  it("supports the mic base-seed pattern (existing query text first)", () => {
    // The mic seeds finalSoFar with the current input value.
    expect(mergeTranscripts("so sánh", "HPG và", "FP")).toBe("so sánh HPG và FP");
  });
});
