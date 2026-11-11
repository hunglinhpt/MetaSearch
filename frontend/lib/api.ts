/**
 * API client for MetaSearch: raw search + SSE answer streaming + LLM configuration.
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
  // Provenance marks; null means unknown, never guessed.
  source_url?: string | null;
  source_name?: string | null;
  data_kind?: "observed" | "derived" | "reference" | "computed" | null;
  as_of?: string | null;
  age_s?: number | null;
};
export type SearchResponse = {
  query_id: string;
  results: SearchHit[];
  engines_used: string[];
  engines_failed: string[];
  latency_ms: number;
  cached: boolean;
  // false when some engine failed or a domain filter removed every result:
  // an empty list is then "not known", not "nothing exists".
  complete?: boolean;
  age_s?: number | null;
};
export type EngineDone = { engine: string; results: number; ms: number };
export type AnswerMeta = {
  answer_id: string;
  latency_ms: number;
  status: string;
  model: string;
  cached?: boolean;
};

export type LLMOverridePayload = {
  provider?: string;
  protocol?: "openai" | "anthropic";
  base_url?: string;
  api_key?: string;
  model?: string;
  quick_model?: string;
  report_model?: string;
  fallback_provider?: string;
  extra_headers?: Record<string, string>;
};

export type LLMProviderPresetInfo = {
  id: string;
  name: string;
  protocol: "openai" | "anthropic" | "none";
  base_url: string;
  default_quick_model: string;
  default_report_model: string;
  is_free: boolean;
  free_tier_type?: string;
  requires_api_key: boolean;
  env_key_name: string;
  signup_url: string;
  docs_url: string;
  description: string;
  recommended_models: string[];
  configured: boolean;
};

export type ActiveLLMConfig = {
  provider: string;
  provider_name: string;
  protocol: string;
  base_url: string;
  quick_model: string;
  report_model: string;
  is_free: boolean;
  requires_api_key: boolean;
  has_api_key: boolean;
  masked_api_key: string;
  fallback_provider: string;
  fallback_models: string[];
  extra_headers_keys: string[];
  has_runtime_override: boolean;
  council_enabled?: boolean;
  council_endpoints?: string[];
  dream_rsi_enabled?: boolean;
};

export type FreeDirectorySource = {
  id: string;
  name: string;
  url: string;
  description: string;
};

export type LLMProvidersResponse = {
  active: ActiveLLMConfig;
  providers: LLMProviderPresetInfo[];
  free_directories?: FreeDirectorySource[];
  external_integration: {
    byo_llm_description: string;
    context_only_endpoint: string;
    citation_validation_endpoint: string;
    council_endpoint?: string;
    dream_rsi_endpoint?: string;
  };
};

export type CouncilPanelist = {
  candidate_id: string;
  provider: string;
  model: string;
  ok: boolean;
  latency_ms: number;
  deterministic_score: number;
  peer_score: number;
  composite_score: number;
  error?: string;
  answer_preview?: string;
};

export type CouncilCrossReview = {
  reviewer_provider: string;
  reviewer_model: string;
  winner_id: string;
  scores: Record<string, number>;
  critique_summary: string;
  latency_ms: number;
};

export type CouncilVerdict = {
  domain: string;
  chairman_model: string;
  consensus_agreement_score: number;
  total_latency_ms: number;
  panelists: CouncilPanelist[];
  cross_reviews: CouncilCrossReview[];
  dream_rsi_policy: Record<string, unknown>;
};

export type DomainProfileInfo = {
  id: string;
  name_vi: string;
  name_en: string;
  category: string;
  maturity: string;
  preferred_engines: string[];
  AnalyticalFrameworks: string[];
  verification_rubric: string[];
};

export type DomainsResponse = {
  flagship_pilot: string;
  total_domains: number;
  domains: DomainProfileInfo[];
};

export type LLMTestResult = {
  ok: boolean;
  provider: string;
  model: string;
  base_url: string;
  reply?: string;
  error?: string;
  latency_ms: number;
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
  onCouncil?: (verdict: CouncilVerdict) => void;
  onDone?: (meta: AnswerMeta) => void;
  onError?: (code: string, message: string) => void;
};

export type SearchOptions = {
  signal?: AbortSignal;
  engines?: string[];
  categories?: string[];
  k?: number;
  freshnessDays?: number;
  // news=true → backend uses the news cache TTL (900s) and the news freshness
  // weight; the news tab always sets it.
  news?: boolean;
};

/** Only http(s) links become hrefs — defense against a misbehaving engine. */
export function safeHref(url: string): string | undefined {
  return /^https?:\/\//i.test(url) ? url : undefined;
}

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
      freshness_days: opts.freshnessDays ?? undefined,
      news: opts.news ?? undefined,
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

// --- server-side TTS (VoiceStudio, docs/17) --------------------------------

export type TtsCapabilities = {
  voicestudio: { enabled: boolean; model: string | null; voice: string | null };
};

// Capabilities are fetched once per page load and shared by every component
// (NewsPanel + the answer header both need them).
let _ttsCapsPromise: Promise<TtsCapabilities> | null = null;

export function ttsCapabilities(signal?: AbortSignal): Promise<TtsCapabilities> {
  if (!_ttsCapsPromise) {
    _ttsCapsPromise = (async () => {
      const resp = await fetch(`${API_BASE_URL}/v1/tts/capabilities`, { signal });
      if (!resp.ok) {
        throw await parseError(resp);
      }
      return resp.json() as Promise<TtsCapabilities>;
    })().catch((err) => {
      _ttsCapsPromise = null; // allow retry on the next mount
      throw err;
    });
  }
  return _ttsCapsPromise;
}

export type TtsSynthesizeOptions = {
  voice?: string;
  model?: string;
  responseFormat?: "mp3" | "wav" | "opus" | "flac" | "pcm";
  signal?: AbortSignal;
};

export type TtsSynthResult = { blob: Blob; etag: string | null };

/** Synthesize speech server-side (VoiceStudio); resolves to audio + its ETag. */
export async function ttsSynthesize(
  text: string,
  opts: TtsSynthesizeOptions = {},
): Promise<TtsSynthResult> {
  const resp = await fetch(`${API_BASE_URL}/v1/tts/synthesize`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      text,
      voice: opts.voice ?? undefined,
      model: opts.model ?? undefined,
      response_format: opts.responseFormat ?? "mp3",
    }),
    signal: opts.signal,
  });
  if (!resp.ok) {
    throw await parseError(resp);
  }
  return { blob: await resp.blob(), etag: resp.headers.get("etag") };
}

export async function listEngines(signal?: AbortSignal): Promise<EngineInfo[]> {
  const resp = await fetch(`${API_BASE_URL}/v1/engines`, { signal });
  if (!resp.ok) {
    throw await parseError(resp);
  }
  return resp.json();
}

export async function getLLMProviders(
  signal?: AbortSignal
): Promise<LLMProvidersResponse> {
  const resp = await fetch(`${API_BASE_URL}/v1/llm/providers`, { signal });
  if (!resp.ok) {
    throw await parseError(resp);
  }
  return resp.json();
}

export async function updateLLMConfig(
  patch: LLMOverridePayload
): Promise<ActiveLLMConfig> {
  const resp = await fetch(`${API_BASE_URL}/v1/llm/config`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!resp.ok) {
    throw await parseError(resp);
  }
  return resp.json();
}

export async function resetLLMConfig(): Promise<ActiveLLMConfig> {
  const resp = await fetch(`${API_BASE_URL}/v1/llm/config/reset`, {
    method: "POST",
  });
  if (!resp.ok) {
    throw await parseError(resp);
  }
  return resp.json();
}

export async function testLLMConnection(
  llm?: LLMOverridePayload
): Promise<LLMTestResult> {
  const resp = await fetch(`${API_BASE_URL}/v1/llm/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(llm ? { llm } : {}),
  });
  if (!resp.ok) {
    throw await parseError(resp);
  }
  return resp.json();
}

export async function getDomains(signal?: AbortSignal): Promise<DomainsResponse> {
  const resp = await fetch(`${API_BASE_URL}/v1/domains`, { signal });
  if (!resp.ok) {
    throw await parseError(resp);
  }
  return resp.json();
}

export async function updateCouncilEndpoints(
  endpoints: string[]
): Promise<{ council_endpoints: string[]; count: number }> {
  const resp = await fetch(`${API_BASE_URL}/v1/llm/council/endpoints`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ endpoints }),
  });
  if (!resp.ok) {
    throw await parseError(resp);
  }
  return resp.json();
}

export async function getDreamRSIState(
  signal?: AbortSignal
): Promise<Record<string, unknown>> {
  const resp = await fetch(`${API_BASE_URL}/v1/llm/dream-rsi/state`, { signal });
  if (!resp.ok) {
    throw await parseError(resp);
  }
  return resp.json();
}

export async function resetDreamRSIState(): Promise<Record<string, unknown>> {
  const resp = await fetch(`${API_BASE_URL}/v1/llm/dream-rsi/reset`, {
    method: "POST",
  });
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
  categories?: string[],
  llm?: LLMOverridePayload,
  councilMode?: boolean,
  councilEndpoints?: string[],
  domain?: string
): Promise<void> {
  const resp = await fetch(`${API_BASE_URL}/v1/answer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      query,
      stream: true,
      engines: engines ?? undefined,
      categories: categories ?? undefined,
      llm: llm ?? undefined,
      council_mode: councilMode ?? undefined,
      council_endpoints: councilEndpoints ?? undefined,
      domain: domain ?? undefined,
    }),
    signal,
  });
  if (!resp.ok || !resp.body) {
    throw await parseError(resp);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  // Same bound as streamSse: a peer that never sends a blank line must not
  // grow the buffer forever.
  const MAX_SSE_BUFFER = 1024 * 1024;

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
      case "council":
        handlers.onCouncil?.(d as unknown as CouncilVerdict);
        break;
      case "done":
        handlers.onDone?.(d as unknown as AnswerMeta);
        break;
      case "error":
        handlers.onError?.(String(d.code ?? "internal"), String(d.message ?? "unknown error"));
        break;
    }
  };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // sse-starlette separates frames with CRLF ("\r\n\r\n"), which contains no
      // "\n\n" — normalize line endings first or every frame piles up in the
      // buffer until the stream closes.
      buffer = buffer.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
      // Overflow check AFTER normalization, so a valid >1MB frame separated
      // only by CRLF does not false-positive.
      if (buffer.length > MAX_SSE_BUFFER && !buffer.includes("\n\n")) {
        throw new Error("SSE stream overflow: no event boundary within 1MB");
      }

      let sep: number;
      // SSE frames are separated by a blank line.
      while ((sep = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        if (frame.length > MAX_SSE_BUFFER) {
          throw new Error("SSE event exceeds 1MB cap");
        }
        dispatchFrame(frame);
      }
    }
  } finally {
    // Always release the connection back to the pool, even on early throw.
    try {
      await reader.cancel();
    } catch {
      // already closed
    }
  }
  // Flush a trailing frame that was not terminated by a blank line.
  if (buffer.trim()) {
    dispatchFrame(buffer.replace(/\r\n/g, "\n").replace(/\r/g, "\n"));
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

export type SuperAgentArtifact = {
  artifact_id: string;
  artifact_type: string;
  title: string;
  content_md: string;
  citations: Citation[];
  metadata?: Record<string, unknown>;
  created_at: number;
};

export type SuperAgentEscalation = {
  escalation_id: string;
  ticket_id: string;
  reason: string;
  proposed_options: string[];
  context_summary: string;
  created_at: number;
  resolved: boolean;
  chosen_option?: string | null;
  resolved_by?: string | null;
  resolved_at?: number | null;
};

export type SuperAgentTicket = {
  ticket_id: string;
  title: string;
  objective: string;
  domain: string;
  priority: number;
  priority_name: string;
  status:
    | "pending"
    | "observed"
    | "running"
    | "waiting_human"
    | "completed"
    | "failed"
    | "cancelled";
  execution_mode: "live" | "observe";
  originating_event_id?: string | null;
  created_at: number;
  started_at?: number | null;
  finished_at?: number | null;
  assigned_worker_id?: string | null;
  step_history: Array<{ step: number; action: string; timestamp: number; [key: string]: unknown }>;
  current_step: number;
  max_steps: number;
  consumed_tokens: number;
  error_message?: string | null;
  artifacts: SuperAgentArtifact[];
  escalations: SuperAgentEscalation[];
};

export type SuperAgentWatchdogStatus = {
  monitored_workers_count: number;
  max_duration_sec: number;
  max_token_budget: number;
  active_metrics: Array<{
    worker_id: string;
    ticket_id: string;
    elapsed_ms: number;
    step_count: number;
    token_burn: number;
    is_healthy: boolean;
    anomalies: string[];
  }>;
  tripped_circuits: Array<{
    worker_id: string;
    ticket_id: string;
    timestamp: number;
    anomalies: string[];
  }>;
};

export async function createSuperAgentTicket(
  req: {
    title: string;
    objective: string;
    domain?: string;
    priority?: number;
    execution_mode?: "live" | "observe";
    run_immediately?: boolean;
  },
  signal?: AbortSignal
): Promise<SuperAgentTicket> {
  const resp = await fetch(`${API_BASE_URL}/v1/superagent/tickets`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(req),
    signal,
  });
  if (!resp.ok) throw await parseError(resp);
  const data = await resp.json();
  return data.ticket as SuperAgentTicket;
}

export async function listSuperAgentTickets(
  filters?: { status?: string; domain?: string },
  signal?: AbortSignal
): Promise<SuperAgentTicket[]> {
  const params = new URLSearchParams();
  if (filters?.status) params.set("status", filters.status);
  if (filters?.domain) params.set("domain", filters.domain);

  const resp = await fetch(`${API_BASE_URL}/v1/superagent/tickets?${params.toString()}`, {
    headers: { Accept: "application/json" },
    signal,
  });
  if (!resp.ok) throw await parseError(resp);
  const data = await resp.json();
  return (data.tickets || []) as SuperAgentTicket[];
}

export async function getSuperAgentTicket(
  ticketId: string,
  signal?: AbortSignal
): Promise<SuperAgentTicket> {
  const resp = await fetch(`${API_BASE_URL}/v1/superagent/tickets/${encodeURIComponent(ticketId)}`, {
    headers: { Accept: "application/json" },
    signal,
  });
  if (!resp.ok) throw await parseError(resp);
  const data = await resp.json();
  return data.ticket as SuperAgentTicket;
}

export async function resolveSuperAgentEscalation(
  ticketId: string,
  escalationId: string,
  chosenOption: string,
  resolvedBy: string = "human_operator",
  signal?: AbortSignal
): Promise<SuperAgentTicket> {
  const resp = await fetch(
    `${API_BASE_URL}/v1/superagent/tickets/${encodeURIComponent(ticketId)}/escalate/${encodeURIComponent(escalationId)}/resolve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ chosen_option: chosenOption, resolved_by: resolvedBy }),
      signal,
    }
  );
  if (!resp.ok) throw await parseError(resp);
  const data = await resp.json();
  return data.ticket as SuperAgentTicket;
}

export async function getSuperAgentWatchdogStatus(
  signal?: AbortSignal
): Promise<SuperAgentWatchdogStatus> {
  const resp = await fetch(`${API_BASE_URL}/v1/superagent/watchdog/status`, {
    headers: { Accept: "application/json" },
    signal,
  });
  if (!resp.ok) throw await parseError(resp);
  return (await resp.json()) as SuperAgentWatchdogStatus;
}

// --- Deep Research (docs/18 §A2): job lifecycle + SSE event stream ----------

export type ResearchCreateResult = { research_id: string; status: string };

export type ResearchStatus = {
  research_id: string;
  status: string;
  progress: number;
  report_md: string;
  citations: Citation[];
  learnings: string[];
  cost_usd: number;
  error: string | null;
};

export type ResearchCreateBody = {
  query: string;
  breadth?: number;
  depth?: number;
  max_time_s?: number;
  k?: number;
};

export async function createResearch(
  body: ResearchCreateBody,
  signal?: AbortSignal
): Promise<ResearchCreateResult> {
  const resp = await fetch(`${API_BASE_URL}/v1/research`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!resp.ok) {
    throw await parseError(resp);
  }
  return resp.json();
}

export async function getResearch(id: string, signal?: AbortSignal): Promise<ResearchStatus> {
  const resp = await fetch(`${API_BASE_URL}/v1/research/${encodeURIComponent(id)}`, { signal });
  if (!resp.ok) {
    throw await parseError(resp);
  }
  return resp.json();
}

export type SseHandlers = {
  onEvent: (name: string, dataJson: string) => void;
  onError?: (err: unknown) => void;
};

/**
 * Minimal SSE reader (backend = sse-starlette, CRLF line endings): parses
 * `event:`/`data:` blocks and hands the RAW data JSON to the caller — the
 * component decides the event vocabulary.
 */
export async function streamSse(
  url: string,
  handlers: SseHandlers,
  signal?: AbortSignal
): Promise<void> {
  const resp = await fetch(url, { signal, headers: { Accept: "text/event-stream" } });
  if (!resp.ok) {
    throw await parseError(resp);
  }
  const reader = resp.body?.getReader();
  if (!reader) {
    handlers.onError?.(new Error("no stream body"));
    return;
  }
  const decoder = new TextDecoder();
  let buffer = "";
  // A peer that never sends a blank line must not grow the buffer forever.
  const MAX_SSE_BUFFER = 1024 * 1024;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // Normalize CRLF (sse-starlette) BEFORE the overflow check, otherwise a
      // valid >1MB block separated only by \r\n\r\n would false-positive.
      buffer = buffer.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
      if (buffer.length > MAX_SSE_BUFFER && !buffer.includes("\n\n")) {
        throw new Error("SSE stream overflow: no event boundary within 1MB");
      }
      let sep = buffer.indexOf("\n\n");
      while (sep >= 0) {
        const block = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        // A single event larger than the cap is refused even though it has a
        // boundary — otherwise one giant frame still buffers whole.
        if (block.length > MAX_SSE_BUFFER) {
          throw new Error("SSE event exceeds 1MB cap");
        }
        let name = "message";
        const dataLines: string[] = [];
        for (const line of block.split("\n")) {
          if (line.startsWith("event:")) name = line.slice(6).trim();
          else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
        }
        if (dataLines.length) handlers.onEvent(name, dataLines.join("\n"));
        sep = buffer.indexOf("\n\n");
      }
    }
  } finally {
    // Always release the connection back to the pool, even on early throw.
    try {
      await reader.cancel();
    } catch {
      /* stream already closed */
    }
  }
}

/** SSE URL for a research job's event stream. */
export function researchEventsUrl(id: string): string {
  return `${API_BASE_URL}/v1/research/${encodeURIComponent(id)}/events`;
}

// --- v2.3.0 market compare + OHLCV chart (docs/18 B1/B2) ---------------------

export type FaField = {
  value: number;
  source: string;
  as_of: string | null;
  validated: boolean;
};

export type FaSymbolResult = {
  symbol: string;
  name: string | null;
  exchange: string | null;
  fields: Record<string, FaField>;
  missing_fields: string[];
  sources_used: string[];
  source_urls: string[];
  as_of: string | null;
  published_at: string | null;
};

export type FaCompareResponse = { results: FaSymbolResult[]; latency_ms: number };

export async function faCompare(
  symbols: string[],
  signal?: AbortSignal
): Promise<FaCompareResponse> {
  const resp = await fetch(`${API_BASE_URL}/v1/fa/compare`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbols }),
    signal,
  });
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export type OhlcvCandle = {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

export type MarketOhlcvResponse = {
  symbol: string;
  candles: OhlcvCandle[];
  source_name: string;
  source_url: string;
  as_of: string | null;
  days: number;
  latency_ms: number;
};

export async function marketOhlcv(
  symbol: string,
  days: number,
  signal?: AbortSignal
): Promise<MarketOhlcvResponse> {
  const resp = await fetch(`${API_BASE_URL}/v1/market/ohlcv`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbol, days }),
    signal,
  });
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

// --- v2.3.0 watchlist + alerts (docs/18 C) ------------------------------------

export type WatchlistItem = {
  id: string;
  query: string;
  label: string | null;
  created_at: string | null;
  last_checked_at: string | null;
  last_hash: string | null;
};

export type WatchlistAlertItem = {
  id: string;
  query: string;
  label: string | null;
  baseline_set: boolean;
  new_items: { url: string; title: string; published_at: string | null; domain: string }[];
  checked_at: string | null;
  error: string | null;
};

export type WatchlistListResponse = {
  items: WatchlistItem[];
  storage: string; // "db" | "memory" — memory means the backend DB is down
};

export type WatchlistAlertsResponse = {
  items: WatchlistAlertItem[];
  total_new: number;
  latency_ms: number;
  storage: string;
};

async function wlFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${API_BASE_URL}${path}`, init);
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export function listWatchlist(signal?: AbortSignal): Promise<WatchlistListResponse> {
  return wlFetch("/v1/watchlist", { signal });
}

export function addWatchlist(
  query: string,
  label?: string,
  signal?: AbortSignal
): Promise<WatchlistItem> {
  return wlFetch("/v1/watchlist", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, label: label ?? null }),
    signal,
  });
}

export function removeWatchlist(id: string, signal?: AbortSignal): Promise<{ deleted: string }> {
  return wlFetch(`/v1/watchlist/${encodeURIComponent(id)}`, { method: "DELETE", signal });
}

export function watchlistAlerts(signal?: AbortSignal): Promise<WatchlistAlertsResponse> {
  return wlFetch("/v1/watchlist/alerts", { signal });
}

// --- v2.3.0 podcast briefing (docs/18 D) ---------------------------------------

export type PodcastBriefingStatus = {
  date: string;
  status: string;
  error: string | null;
  chars: number;
  audio_bytes: number;
  created_at: string | null;
};

export async function createPodcastBriefing(signal?: AbortSignal): Promise<PodcastBriefingStatus> {
  const resp = await fetch(`${API_BASE_URL}/v1/podcast/briefing`, {
    method: "POST",
    signal,
  });
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export async function podcastBriefingStatus(
  signal?: AbortSignal
): Promise<PodcastBriefingStatus> {
  const resp = await fetch(`${API_BASE_URL}/v1/podcast/briefing/status`, { signal });
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export function podcastLatestUrl(): string {
  return `${API_BASE_URL}/v1/podcast/briefing/latest`;
}

export function podcastFeedUrl(): string {
  return `${API_BASE_URL}/v1/podcast/briefing/feed.xml`;
}
