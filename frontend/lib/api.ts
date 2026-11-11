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
