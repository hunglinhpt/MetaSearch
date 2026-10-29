/**
 * API client for MetaSearch: raw search + SSE answer streaming.
 *
 * POST /v1/answer with stream:true emits `event:`/`data:` SSE frames; browsers
 * cannot use EventSource for POST, so we parse the fetch stream by hand.
 */

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export type Citation = { n: number; url: string; title: string; chunk_id?: string };
export type SourceItem = { url: string; title: string; domain: string; score?: number };
export type SearchHit = {
  rank: number;
  url: string;
  title: string;
  snippet: string;
  score: number;
  consensus: number;
  found_by: string[];
  engines?: string[];
  domain: string;
  published_at?: string | null;
};
export type SearchResponse = {
  query_id: string;
  results: SearchHit[];
  engines_used: string[];
  engines_failed: string[];
  latency_ms: number;
  cached: boolean;
};
export type EngineDone = { engine: string; results: number; ms: number };
export type AnswerMeta = {
  answer_id: string;
  latency_ms: number;
  status: string;
  model: string;
  cached?: boolean;
};

export type AnswerHandlers = {
  onStatus?: (stage: string, detail: string) => void;
  onEngineDone?: (info: EngineDone) => void;
  onSources?: (sources: SourceItem[]) => void;
  onToken?: (text: string) => void;
  onCitations?: (citations: Citation[]) => void;
  onDone?: (meta: AnswerMeta) => void;
  onError?: (message: string) => void;
};

export async function search(
  query: string,
  signal?: AbortSignal
): Promise<SearchResponse> {
  const resp = await fetch(`${API_BASE_URL}/v1/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, k: 10 }),
    signal,
  });
  if (!resp.ok) {
    throw new Error(await extractError(resp));
  }
  return resp.json();
}

export async function streamAnswer(
  query: string,
  handlers: AnswerHandlers,
  signal?: AbortSignal
): Promise<void> {
  const resp = await fetch(`${API_BASE_URL}/v1/answer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, stream: true }),
    signal,
  });
  if (!resp.ok || !resp.body) {
    throw new Error(await extractError(resp));
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const dispatch = (eventName: string, dataText: string) => {
    if (!eventName || !dataText) return;
    let data: unknown;
    try {
      data = JSON.parse(dataText);
    } catch {
      return;
    }
    const d = data as Record<string, unknown>;
    switch (eventName) {
      case "status":
        handlers.onStatus?.(String(d.stage ?? ""), String(d.detail ?? ""));
        break;
      case "engine_done":
        handlers.onEngineDone?.(d as unknown as EngineDone);
        break;
      case "sources":
        handlers.onSources?.((d.sources as SourceItem[]) ?? []);
        break;
      case "token":
        handlers.onToken?.(String(d.t ?? ""));
        break;
      case "citations":
        handlers.onCitations?.((d.citations as Citation[]) ?? []);
        break;
      case "done":
        handlers.onDone?.(d as unknown as AnswerMeta);
        break;
      case "error":
        handlers.onError?.(String(d.message ?? "unknown error"));
        break;
    }
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let sep: number;
    // SSE frames are separated by a blank line.
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      dispatchFrame(frame);
    }
  }
  // Flush a trailing frame that was not terminated by a blank line.
  if (buffer.trim()) {
    dispatchFrame(buffer);
  }

  function dispatchFrame(frame: string) {
    let eventName = "";
    const dataLines: string[] = [];
    for (const line of frame.split("\n")) {
      if (line.startsWith(":")) continue; // SSE comment / keepalive ping
      if (line.startsWith("event:")) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trim());
      }
    }
    dispatch(eventName, dataLines.join("\n"));
  }
}

async function extractError(resp: Response): Promise<string> {
  try {
    const body = await resp.json();
    return body?.error?.message ?? `HTTP ${resp.status}`;
  } catch {
    return `HTTP ${resp.status}`;
  }
}
