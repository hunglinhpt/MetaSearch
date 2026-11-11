import { beforeEach, describe, expect, it, vi } from "vitest";

import { chunkForSpeech, stripForSpeech } from "./tts";

describe("stripForSpeech", () => {
  it("removes citation markers [n]", () => {
    expect(stripForSpeech("VN-Index tăng 1,2% [1][12].")).toBe("VN-Index tăng 1,2% .");
  });

  it("keeps the link label only, never the URL", () => {
    expect(stripForSpeech("xem [SSI Research](https://www.ssi.com.vn/r) tại đây")).toBe(
      "xem SSI Research tại đây",
    );
  });

  it("link rule runs before citation rule: [12](url) leaves no orphan paren", () => {
    expect(stripForSpeech("theo [12](https://a.b) nguồn")).toBe("theo 12 nguồn");
  });

  it("strips bare URLs entirely", () => {
    expect(stripForSpeech("Nguồn: https://vnexpress.net/x?utm=1 xem tiếp.")).toBe(
      "Nguồn: xem tiếp.",
    );
  });

  it("tolerates one level of nested parens in link URLs", () => {
    expect(stripForSpeech("[Hà Nội](https://vi.wikipedia.org/wiki/Hà_Nội_(thủ_đô))")).toBe(
      "Hà Nội",
    );
  });

  it("drops fenced code, images, headings, emphasis, table separators", () => {
    const md = [
      "## Tiêu đề",
      "```js",
      "const x = 1;",
      "```",
      "![ảnh](https://a.b/img.png)",
      "**đậm** và `code` và ~~gạch~~",
      "| A | B |",
      "|---|---|",
      "| 1 | 2 |",
    ].join("\n");
    const out = stripForSpeech(md);
    expect(out).not.toContain("const x");
    expect(out).not.toContain("https://");
    expect(out).not.toContain("---");
    expect(out).not.toContain("**");
    expect(out).toContain("Tiêu đề");
    expect(out).toContain("đậm");
    expect(out).toContain("A B");
  });

  it("unescapes common entities and strips zero-width chars and numbered bullets", () => {
    expect(stripForSpeech("Tom &amp; Jerry&nbsp;1. Mục một")).toBe("Tom & Jerry 1. Mục một");
    expect(stripForSpeech("a\u200Bb")).toBe("ab");
    // numbered list marker is stripped, content kept
    expect(stripForSpeech("1. Tin một\n2. Tin hai")).toBe("Tin một Tin hai");
  });

  it("returns empty for markup-only input", () => {
    expect(stripForSpeech("![x](https://a.b)")).toBe("");
  });
});

describe("chunkForSpeech", () => {
  it("packs sentences under maxLen", () => {
    const chunks = chunkForSpeech("Một. Hai. Ba.", 10);
    expect(chunks.length).toBeGreaterThan(1);
    expect(chunks.join(" ")).toBe("Một. Hai. Ba.");
  });

  it("hard-splits a single spaceless word longer than maxLen", () => {
    const word = "a".repeat(500);
    const chunks = chunkForSpeech(word, 220);
    expect(chunks.length).toBe(3);
    expect(chunks.every((c) => c.length <= 220)).toBe(true);
    expect(chunks.join("")).toBe(word);
  });

  it("clamps a non-positive maxLen instead of looping forever", () => {
    const chunks = chunkForSpeech("abc def", 0);
    expect(chunks.length).toBeGreaterThan(0);
    expect(chunks.every((c) => c.length <= 1)).toBe(true);
  });

  it("does not split inside Vietnamese abbreviations like TP.HCM", () => {
    const chunks = chunkForSpeech("Thị trường TP.HCM tăng. Kết thúc.", 220);
    expect(chunks).toEqual(["Thị trường TP.HCM tăng. Kết thúc."]);
  });
});

// --- engine selection + remote (VoiceStudio) speak (docs/17) ---------------

vi.mock("./api", () => ({
  ttsSynthesize: vi.fn(async (text: string) => ({ blob: new Blob([text], { type: "audio/mpeg" }), etag: "\"e\"" })),
}));

import { ttsSynthesize } from "./api";
import { getPreferredEngine, setPreferredEngine, ttsSpeakRemote } from "./tts";

function stubBrowserGlobals() {
  const store = new Map<string, string>();
  (globalThis as any).window = {
    localStorage: {
      getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
      clear: () => store.clear(),
    },
  };
}

class FakeAudio {
  static instances: FakeAudio[] = [];
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;
  paused = false;
  constructor(public src: string) {
    FakeAudio.instances.push(this);
  }
  play() {
    return Promise.resolve();
  }
  pause() {
    this.paused = true;
  }
}

describe("TTS engine preference", () => {
  beforeEach(() => {
    stubBrowserGlobals();
    (globalThis as any).window.localStorage.clear();
  });

  it("defaults to webspeech", () => {
    expect(getPreferredEngine()).toBe("webspeech");
  });

  it("persists the voicestudio choice", () => {
    setPreferredEngine("voicestudio");
    expect(getPreferredEngine()).toBe("voicestudio");
  });
});

describe("ttsSpeakRemote (VoiceStudio server-side)", () => {
  beforeEach(() => {
    stubBrowserGlobals();
    FakeAudio.instances = [];
    (globalThis as any).Audio = FakeAudio;
    (globalThis as any).URL.createObjectURL = () => "blob:fake";
    (globalThis as any).URL.revokeObjectURL = () => {};
    vi.mocked(ttsSynthesize).mockClear();
  });

  it("plays chunks in order; each chunk is synthesized exactly once (prefetch+cache)", async () => {
    const sentence = "Mot cau tin tuc kha dai de van ban vuot qua nguong hai tram hai muoi ky tu khi doc.";
    const text = Array.from({ length: 6 }, (_, i) => `${sentence} ${i + 1}.`).join(" ");
    const expected = chunkForSpeech(stripForSpeech(text));
    expect(expected.length).toBeGreaterThan(1);
    const synthCalls: string[] = [];
    vi.mocked(ttsSynthesize).mockImplementation(async (t: string) => {
      synthCalls.push(t);
      return { blob: new Blob([t]), etag: "e" };
    });
    const urlToBlob = new Map<string, Blob>();
    let n = 0;
    (globalThis as any).URL.createObjectURL = (b: Blob) => {
      const u = `blob:${n++}`;
      urlToBlob.set(u, b);
      return u;
    };
    const played: Promise<string>[] = [];
    (globalThis as any).Audio = class extends FakeAudio {
      play() {
        played.push(urlToBlob.get(this.src)!.text());
        return Promise.resolve();
      }
    };
    const events: string[] = [];
    const handle = ttsSpeakRemote(text, {
      onStart: () => events.push("start"),
      onEnd: () => events.push("end"),
    });
    for (let i = 0; i < expected.length; i++) {
      await vi.waitFor(() => expect(FakeAudio.instances.length).toBe(i + 1));
      FakeAudio.instances[i].onended!();
    }
    await vi.waitFor(() => expect(events).toContain("end"));
    // playback order == chunk order
    expect(await Promise.all(played)).toEqual(expected);
    // every chunk synthesized exactly once (prefetch fills the cache, the
    // sequential player then cache-hits instead of re-calling the network)
    expect(synthCalls.length).toBe(expected.length);
    expect(new Set(synthCalls)).toEqual(new Set(expected));
    expect(events[0]).toBe("start");
    handle?.cancel();
  });

  it("cancel stops the chain: no further synthesis, no onEnd", async () => {
    const text = Array(6).fill("Câu tin ngắn gọn cho nhiều chunk.").join(" ");
    const calls: string[] = [];
    vi.mocked(ttsSynthesize).mockImplementation(async (t: string) => {
      calls.push(t);
      return { blob: new Blob([t]), etag: "e" };
    });
    let ended = false;
    const handle = ttsSpeakRemote(text, { onEnd: () => (ended = true) });
    await vi.waitFor(() => expect(calls.length).toBe(1));
    handle?.cancel();
    FakeAudio.instances[0]?.onended?.(); // stale end after cancel must be ignored
    await new Promise((r) => setTimeout(r, 20));
    expect(calls.length).toBe(1);
    expect(ended).toBe(false);
  });

  it("reports upstream failure via onError and stops the chain", async () => {
    vi.mocked(ttsSynthesize).mockRejectedValueOnce(new Error("tts_disabled"));
    const errors: string[] = [];
    let ended = false;
    ttsSpeakRemote("Tin mới.", {
      onError: (r) => errors.push(r),
      onEnd: () => (ended = true),
    });
    await vi.waitFor(() => expect(errors).toContain("tts_disabled"));
    expect(ended).toBe(false);
  });
});
