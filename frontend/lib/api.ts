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

/**
 * Common client error mirroring the backend error model
 * (api/app/core/errors.py): invalid_request | unauthorized | rate_limited |
 * upstream_timeout | no_results | internal.
 */
export class ApiError extends Error {
  code: string;
  status: number;

  constructor(code: string, message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

export type AnswerHandlers = {
  onStatus?: (stage: string, detail: string) => void;
  onEngineDone?: (info: EngineDone) => void;
  onSources?: (sources: SourceItem[]) => void;
  onToken?: (text: string) => void;
  onCitations?: (citations: Citation[]) => void;
  onDone?: (meta: AnswerMeta) => void;
  onError?: (code: string, message: string) => void;
};

export type SearchOptions = {
  signal?: AbortSignal;
  engines?: string[];
  categories?: string[];
  k?: number;
};

export async function search(
  query: string,
  opts: SearchOptions = {}
): Promise<SearchResponse> {
  const resp = await fetch(`${API_BASE_URL}/v1/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      query,
      k: opts.k ?? 10,
      engines: opts.engines ?? undefined,
      categories: opts.categories ?? undefined,
    }),
    signal: opts.signal,
  });
  if (!resp.ok) {
    throw await parseError(resp);
  }
  return resp.json();
}

export type EngineInfo = {
  id: string;
  name: string;
  category?: string;
  weight?: number;
  enabled: boolean;
  adapter_ready: boolean;
  disabled_reason?: string | null;
};

export async function listEngines(signal?: AbortSignal): Promise<EngineInfo[]> {
  const resp = await fetch(`${API_BASE_URL}/v1/engines`, { signal });
  if (!resp.ok) {
    throw await parseError(resp);
  }
  return resp.json();
}

export async function streamAnswer(
  query: string,
  handlers: AnswerHandlers,
  signal?: AbortSignal,
  engines?: string[],
  categories?: string[]
): Promise<void> {
  const resp = await fetch(`${API_BASE_URL}/v1/answer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      query,
      stream: true,
      engines: engines ?? undefined,
      categories: categories ?? undefined,
    }),
    signal,
  });
  if (!resp.ok || !resp.body) {
    throw await parseError(resp);
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
        handlers.onError?.(String(d.code ?? "internal"), String(d.message ?? "unknown error"));
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

async function parseError(resp: Response): Promise<ApiError> {
  try {
    const body = await resp.json();
    const err = body?.error ?? {};
    return new ApiError(
      String(err.code ?? "internal"),
      String(err.message ?? `HTTP ${resp.status}`),
      resp.status
    );
  } catch {
    return new ApiError("internal", `HTTP ${resp.status}`, resp.status);
  }
}

export type BlogPostSummary = {
  id: string;
  slug: string;
  topic: string;
  title: string;
  summary?: string | null;
  tags?: string[] | null;
  published_at?: string | null;
  created_at?: string | null;
};

export type BlogPostDetail = BlogPostSummary & {
  content_md: string;
  sources?: { n: number; title: string; url: string; snippet?: string }[] | null;
};

export type ScanTopicInfo = {
  id: string;
  name: string;
  query: string;
  category: string;
  tags: string[];
  description: string;
  last_run?: string | null;
  last_post_id?: string | null;
};

export async function listDigests(
  topic?: string,
  limit: number = 20,
  signal?: AbortSignal
): Promise<BlogPostSummary[]> {
  const params = new URLSearchParams();
  if (topic) params.set("topic", topic);
  params.set("limit", String(limit));

  const resp = await fetch(`${API_BASE_URL}/v1/digests?${params.toString()}`, {
    headers: { Accept: "application/json" },
    signal,
  });
  if (!resp.ok) throw await parseError(resp);
  return (await resp.json()) as BlogPostSummary[];
}

export async function getDigest(
  slugOrId: string,
  signal?: AbortSignal
): Promise<BlogPostDetail> {
  const resp = await fetch(`${API_BASE_URL}/v1/digests/${encodeURIComponent(slugOrId)}`, {
    headers: { Accept: "application/json" },
    signal,
  });
  if (!resp.ok) throw await parseError(resp);
  return (await resp.json()) as BlogPostDetail;
}

export async function listTopics(signal?: AbortSignal): Promise<ScanTopicInfo[]> {
  const resp = await fetch(`${API_BASE_URL}/v1/digests/topics`, {
    headers: { Accept: "application/json" },
    signal,
  });
  if (!resp.ok) throw await parseError(resp);
  return (await resp.json()) as ScanTopicInfo[];
}

export async function triggerScan(
  topic?: string,
  signal?: AbortSignal
): Promise<BlogPostSummary[]> {
  const resp = await fetch(`${API_BASE_URL}/v1/digests/scan`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(topic ? { topic } : {}),
    signal,
  });
  if (!resp.ok) throw await parseError(resp);
  return (await resp.json()) as BlogPostSummary[];
}
