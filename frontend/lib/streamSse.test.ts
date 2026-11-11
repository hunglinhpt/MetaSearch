import { afterEach, describe, expect, it, vi } from "vitest";

import { streamAnswer, streamSse } from "./api";

/** Build a fetch stub that answers one SSE body, chunked as given. */
function stubSseFetch(chunks: string[], status = 200) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(encoder.encode(c));
      controller.close();
    },
  });
  return vi.fn(async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => ({ error: { code: "x", message: "m" } }),
    body: status === 200 ? { getReader: () => stream.getReader() } : null,
  }));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("streamSse", () => {
  it("parses CRLF-separated events with multi-line data", async () => {
    vi.stubGlobal(
      "fetch",
      stubSseFetch(["event: status\r\ndata: {\"stage\":\"a\"}\r\n\r\nevent: report_token\r\ndata: {\"t\":\"he\"}"], )
    );
    // second event split across chunks, plus a multi-line data block
    vi.stubGlobal(
      "fetch",
      stubSseFetch([
        "event: status\r\ndata: {\"stage\":\"a\"}\r\n\r\n",
        "event: report_tok",
        "en\r\ndata: {\"t\":\"he\"}\r\ndata: {\"t\":\"llo\"}\r\n\r\n",
      ])
    );
    const events: [string, string][] = [];
    await streamSse("http://x/sse", { onEvent: (n, d) => events.push([n, d]) });
    expect(events).toEqual([
      ["status", '{"stage":"a"}'],
      ["report_token", '{"t":"he"}\n{"t":"llo"}'],
    ]);
  });

  it("ignores blocks without data and defaults the event name to message", async () => {
    vi.stubGlobal("fetch", stubSseFetch(["event: ping\r\n\r\ndata: hello\n\n"]));
    const events: [string, string][] = [];
    await streamSse("http://x/sse", { onEvent: (n, d) => events.push([n, d]) });
    expect(events).toEqual([["message", "hello"]]);
  });

  it("caps buffer AND single events at 1MB (CRLF normalized first — no false positive)", async () => {
    const big = "x".repeat(1024 * 1024 + 10);
    // one giant event WITH a boundary is still refused (per-block cap)
    vi.stubGlobal("fetch", stubSseFetch([`data: ${big}\r\n\r\n`]));
    await expect(
      streamSse("http://x/sse", { onEvent: () => {} })
    ).rejects.toThrow("SSE event exceeds 1MB cap");
    // no boundary at all -> buffer overflow guard fires
    vi.stubGlobal("fetch", stubSseFetch([big]));
    await expect(
      streamSse("http://x/sse", { onEvent: () => {} })
    ).rejects.toThrow("SSE stream overflow");
    // a normal-size CRLF stream still parses fine
    vi.stubGlobal("fetch", stubSseFetch(['data: {"ok":1}\r\n\r\n']));
    const events: [string, string][] = [];
    await streamSse("http://x/sse", { onEvent: (n, d) => events.push([n, d]) });
    expect(events).toHaveLength(1);
  });

  it("throws the API error on non-200", async () => {
    vi.stubGlobal("fetch", stubSseFetch([], 503));
    await expect(streamSse("http://x/sse", { onEvent: () => {} })).rejects.toThrow();
  });
});

describe("streamAnswer (CRLF frames from sse-starlette)", () => {
  it("dispatches every token/status/done event when frames use \\r\\n\\r\\n", async () => {
    // This is the exact wire format of sse-starlette 3.7.0 ServerSentEvent.encode()
    const wire =
      'event: status\r\ndata: {"stage":"search"}\r\n\r\n' +
      'event: token\r\ndata: {"t":"Xin "}\r\n\r\n' +
      'event: token\r\ndata: {"t":"chào"}\r\n\r\n' +
      'event: done\r\ndata: {"answer_id":"a1"}\r\n\r\n';
    vi.stubGlobal("fetch", stubSseFetch([wire]));
    const tokens: string[] = [];
    const statuses: string[] = [];
    let doneMeta: unknown = null;
    await streamAnswer("q", {
      onToken: (t) => tokens.push(t),
      onStatus: (s) => statuses.push(s),
      onDone: (m) => {
        doneMeta = m;
      },
    });
    expect(tokens).toEqual(["Xin ", "chào"]);
    expect(statuses).toEqual(["search"]);
    expect(doneMeta).toEqual({ answer_id: "a1" });
  });
});
