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
  // v2.5 P0-7 agent extras (only when requested via SearchOptions.extras)
  contents?: string;
  highlights?: string[];
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
  // v2.5 P0-7: instant answer over the top results (extras=["answer"])
  answer?: string | null;
};
export type EngineDone = { engine: string; results: number; ms: number };
export type AnswerMeta = {
  answer_id: string;
  latency_ms: number;
  status: string;
  model: string;
  cached?: boolean;
  // v2.5 P0-5: suggested follow-up questions (SSE `follow_ups` event / done payload)
  follow_ups?: string[];
  // v3.2 AI Infrastructure: selected skill + Headroom compression stats
  skill?: {
    skill_id: string;
    domain_id: string;
    domain_name: string;
    complexity: string;
    recommended_mode: string;
    compression_profile: string;
  };
  compression?: {
    compressed: boolean;
    raw_units: number;
    compressed_units: number;
    saved_units: number;
    saved_pct: number;
    chunks_in: number;
    chunks_out: number;
  };
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

export type LLMKeyValidationItem = {
  provider: string;
  name: string;
  protocol: string;
  base_url: string;
  requires_api_key: boolean;
  free_tier_type: string;
  configured: boolean;
  format_valid: boolean;
  format_reason: string;
  masked_key: string;
  key_valid: boolean | null;
  reachable: boolean | null;
  rate_limited: boolean;
  status_code: number | null;
  latency_ms: number | null;
  models_count: number | null;
  error: string | null;
};

export type LLMValidateResponse = {
  dry_run: boolean;
  total_checked: number;
  configured_count: number;
  valid_count: number;
  invalid_count: number;
  unreachable_count: number;
  results: LLMKeyValidationItem[];
};

export type LLMHealthProviderItem = {
  provider: string;
  score: number;
  configured: boolean;
  format_valid: boolean;
  key_valid: boolean | null;
  breaker_state: string;
  ewma_latency_ms: number | null;
  consecutive_failures: number;
  free_tier_type: string;
};

export type LLMHealthResponse = {
  primary_provider: string;
  explicit_fallback_provider: string | null;
  auto_fallback_enabled: boolean;
  allow_keyless_fallback: boolean;
  fallback_chain: string[];
  ranked_providers: LLMHealthProviderItem[];
};

export type SkillSummary = {
  id: string;
  domain_id: string;
  name: string;
  description: string;
  compression_profile: string;
  preferred_engines: string[];
};

export type SkillsResponse = {
  enabled: boolean;
  total: number;
  skills: SkillSummary[];
  routed?: Record<string, unknown>;
};

export type SkillTelemetryResponse = {
  enabled: boolean;
  total_recorded: number;
  skills: Record<string, Record<string, unknown>>;
};

export type MemoryIndexItem = {
  id: string;
  kind: string;
  title: string;
  snippet: string;
  score: number;
  created_at?: string | null;
};

export type MemorySearchResponse = {
  query: string;
  items: MemoryIndexItem[];
  storage: string;
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
  // docs/30 BP-4: the full citation map arrives BEFORE the token stream, so the
  // UI can render clickable chips immediately while `[n]` markers stream in.
  onCitationMap?: (map: Citation[]) => void;
  // docs/30 BP-1: the follow-up question was rewritten standalone using the
  // prior thread answer (lets the UI show what was actually searched).
  onThreadRewrite?: (original: string, rewritten: string) => void;
  onCouncil?: (verdict: CouncilVerdict) => void;
  onFollowUps?: (items: string[]) => void;
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
  // docs/19: lang ("vi"/"en"/…) plumbed to engines (searxng language, ddg region,
  // wikipedia subdomain, google news hl/gl, gdelt sourcelang, openalex filter).
  lang?: string;
  // docs/19 W4: deep=true fans out to ALL enabled engines (category filter off).
  deep?: boolean;
  // v2.5 P0-7: agent extras on the top results (contents/highlights/answer).
  extras?: ("contents" | "highlights" | "answer")[];
};

/** Only http(s) links become hrefs — defense against a misbehaving engine.
 * Audit round 2: absurdly long URLs (>2048, browser limits) are dropped. */
export function safeHref(url: string): string | undefined {
  if (url.length > 2048) return undefined;
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
      lang: opts.lang ?? undefined,
      deep: opts.deep ?? undefined,
      extras: opts.extras ?? undefined,
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
    // The shared promise must NOT bind the first caller's AbortSignal (audit
    // round 12): a later sharer would inherit its abort. Capabilities is a
    // tiny one-shot GET — always fetch it signal-free once per page load.
    _ttsCapsPromise = (async () => {
      const resp = await fetch(`${API_BASE_URL}/v1/tts/capabilities`);
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

export async function validateLLMKeys(opts?: {
  providers?: string[];
  include_unconfigured?: boolean;
  dry_run?: boolean;
  timeout_s?: number;
}): Promise<LLMValidateResponse> {
  const resp = await fetch(`${API_BASE_URL}/v1/llm/validate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(opts ?? {}),
  });
  if (!resp.ok) {
    throw await parseError(resp);
  }
  return resp.json();
}

export async function getLLMHealth(
  task = "answer",
  signal?: AbortSignal
): Promise<LLMHealthResponse> {
  const resp = await fetch(`${API_BASE_URL}/v1/llm/health?task=${encodeURIComponent(task)}`, {
    signal,
  });
  if (!resp.ok) {
    throw await parseError(resp);
  }
  return resp.json();
}

export async function listSkills(
  query?: string,
  signal?: AbortSignal
): Promise<SkillsResponse> {
  const qs = query ? `?q=${encodeURIComponent(query)}` : "";
  const resp = await fetch(`${API_BASE_URL}/v1/skills${qs}`, { signal });
  if (!resp.ok) {
    throw await parseError(resp);
  }
  return resp.json();
}

// --- Expert briefings (docs/33) ------------------------------------------------

export type BriefingSource = {
  n: number;
  title: string;
  url: string;
  snippet?: string;
  published?: string | null;
};

export type BriefingDetail = {
  id: string;
  slug: string;
  topic: string;
  title: string;
  summary: string | null;
  content_md: string;
  tags: string[] | null;
  sources: BriefingSource[] | null;
  published_at: string | null;
};

export type BriefingChannelInfo = {
  id: string;
  name: string;
  kind: string;
  window_hours: number;
  persona_name: string;
  description: string;
  tags: string[];
  enabled: boolean;
  freshness_days: number;
};

export type BriefingChannelsResponse = {
  news_channels: BriefingChannelInfo[];
  deep_channels: BriefingChannelInfo[];
  ai_enabled: boolean;
  review_enabled: boolean;
};

export async function listBriefingChannels(
  signal?: AbortSignal
): Promise<BriefingChannelsResponse> {
  const resp = await fetch(`${API_BASE_URL}/v1/briefings/channels`, { signal });
  if (!resp.ok) {
    throw await parseError(resp);
  }
  return resp.json();
}

export async function generateBriefing(
  channelId: string,
  signal?: AbortSignal
): Promise<BriefingDetail> {
  const resp = await fetch(
    `${API_BASE_URL}/v1/briefings/${encodeURIComponent(channelId)}`,
    { method: "POST", signal }
  );
  if (!resp.ok) {
    throw await parseError(resp);
  }
  return resp.json();
}

export async function latestBriefing(
  channelId: string,
  signal?: AbortSignal
): Promise<BriefingDetail> {
  const resp = await fetch(
    `${API_BASE_URL}/v1/briefings/${encodeURIComponent(channelId)}/latest`,
    { signal }
  );
  if (!resp.ok) {
    throw await parseError(resp);
  }
  return resp.json();
}

export async function getSkillTelemetry(
  signal?: AbortSignal
): Promise<SkillTelemetryResponse> {
  const resp = await fetch(`${API_BASE_URL}/v1/ops/skills`, { signal });
  if (!resp.ok) {
    throw await parseError(resp);
  }
  return resp.json();
}

export async function searchMemoryIndex(
  q: string,
  opts?: { kind?: string; limit?: number; signal?: AbortSignal }
): Promise<MemorySearchResponse> {
  const params = new URLSearchParams({ q, limit: String(opts?.limit ?? 10) });
  if (opts?.kind) params.set("kind", opts.kind);
  const resp = await fetch(`${API_BASE_URL}/v1/memory/search?${params.toString()}`, {
    signal: opts?.signal,
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
  domain?: string,
  lang?: string,
  deep?: boolean,
  k?: number
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
      lang: lang ?? undefined,
      deep: deep ?? undefined,
      k: k ?? undefined,
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
      case "citation_map":
        handlers.onCitationMap?.((d.map as Citation[]) ?? []);
        break;
      case "thread_rewrite":
        handlers.onThreadRewrite?.(String(d.original ?? ""), String(d.rewritten ?? ""));
        break;
      case "council":
        handlers.onCouncil?.(d as unknown as CouncilVerdict);
        break;
      case "follow_ups":
        handlers.onFollowUps?.((d.follow_ups as string[]) ?? []);
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

export type ResearchConflict = {
  topic: string;
  claim_a: string;
  citation_a: number | null;
  claim_b: string;
  citation_b: number | null;
};

export type ResearchVerification = {
  citation_count: number;
  invalid_markers: number[];
  unsupported_markers: number[];
  flagged_sentences?: string[];
  numeric_fidelity: number;
  unverified_numbers?: string[];
  citation_coverage: number;
  conflicts_count: number;
};

export type ResearchToolSummary = {
  tool: string;
  items?: number;
  events?: number;
  chunks?: number;
  space?: string;
  symbols?: string[];
};

export type ResearchHistoryItem = {
  research_id: string;
  query: string;
  status: string;
  profile?: string | null;
  cost_usd: number;
  created_at?: string | null;
  in_memory?: boolean;
};

export type ResearchStatus = {
  research_id: string;
  status: string;
  progress: number | { iteration: number; max_iterations: number };
  report_md: string;
  citations: Citation[];
  learnings: string[];
  outline?: string[];
  conflicts?: ResearchConflict[];
  verification?: ResearchVerification | null;
  tools?: ResearchToolSummary[];
  profile?: string | null;
  cost_usd: number;
  error: string | null;
};

export type ResearchCreateBody = {
  query: string;
  breadth?: number;
  depth?: number;
  max_time_s?: number;
  k?: number;
  profile?: string;
  space?: string;
};

export async function listResearchJobs(
  limit = 20,
  signal?: AbortSignal
): Promise<{ jobs: ResearchHistoryItem[] }> {
  const resp = await fetch(`${API_BASE_URL}/v1/research?limit=${encodeURIComponent(String(limit))}`, {
    signal,
  });
  if (!resp.ok) {
    throw await parseError(resp);
  }
  return resp.json();
}

export async function cancelResearchJob(
  id: string,
  signal?: AbortSignal
): Promise<ResearchCreateResult> {
  const resp = await fetch(
    `${API_BASE_URL}/v1/research/${encodeURIComponent(id)}/cancel`,
    { method: "POST", signal }
  );
  if (!resp.ok) {
    throw await parseError(resp);
  }
  return resp.json();
}

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
/**
 * Audit round 12: find the end of the first SSE frame in RAW text (a blank
 * line, CRLF or LF) WITHOUT normalizing chunks first — returns the index past
 * the boundary plus the LF-normalized frame. Normalizing whole frames prevents
 * a "\r\n\r\n" boundary straddling a TCP chunk from mangling into "\n\n\n".
 */
function searchFrameEnd(
  raw: string
): { end: number; normalized: string } | null {
  const crlf = raw.indexOf("\r\n\r\n");
  const lf = raw.indexOf("\n\n");
  if (crlf >= 0 && (lf < 0 || crlf + 2 <= lf + 1)) {
    return { end: crlf + 4, normalized: raw.slice(0, crlf).replace(/\r\n/g, "\n") };
  }
  if (lf >= 0) {
    return { end: lf + 2, normalized: raw.slice(0, lf) };
  }
  return null;
}

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
      // Audit round 12: find the frame boundary in the RAW buffer — a
      // "\r\n\r\n" boundary split across a TCP chunk must not be mangled
      // by per-chunk CRLF normalization (it could dispatch a headless event).
      if (buffer.length > MAX_SSE_BUFFER && !/\r?\n\r?\n/.test(buffer)) {
        throw new Error("SSE stream overflow: no event boundary within 1MB");
      }
      let frameEnd = searchFrameEnd(buffer);
      while (frameEnd) {
        const block = frameEnd.normalized;
        buffer = buffer.slice(frameEnd.end);
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
        frameEnd = searchFrameEnd(buffer);
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

/** SSE URL for a research job's event stream (optionally resuming after sequence id). */
export function researchEventsUrl(id: string, afterSeq?: number): string {
  const base = `${API_BASE_URL}/v1/research/${encodeURIComponent(id)}/events`;
  return typeof afterSeq === "number" && afterSeq > 0 ? `${base}?after_seq=${afterSeq}` : base;
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

// --- Personalized Watchlist Podcast (/v1/podcast/personal*) ------------------

export async function createPersonalPodcast(signal?: AbortSignal): Promise<PodcastBriefingStatus> {
  const resp = await fetch(`${API_BASE_URL}/v1/podcast/personal`, {
    method: "POST",
    signal,
  });
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export async function personalPodcastStatus(
  signal?: AbortSignal
): Promise<PodcastBriefingStatus> {
  const resp = await fetch(`${API_BASE_URL}/v1/podcast/personal/status`, { signal });
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export function personalPodcastLatestUrl(): string {
  return `${API_BASE_URL}/v1/podcast/personal/latest`;
}

// --- Omni-Channel Bilingual Equity Sentiment Engine (/v1/market/sentiment) ---

export type MarketSentimentRequest = {
  ticker: string;
  company_name?: string;
  exchange?: "HOSE" | "HSX" | "HNX" | "UPCOM" | "NASDAQ" | "NYSE" | "OTHER";
  k?: number;
  max_queries?: number;
  fetch_market_data?: boolean;
  use_llm_judge?: boolean;
  timeout_ms?: number;
};

export type SentimentPillarItem = {
  pillar_id: string;
  name: string;
  standard_ref: string;
  score_0_100: number;
  status: "BULLISH" | "BEARISH" | "NEUTRAL" | "WARNING" | "INSUFFICIENT_DATA";
  evidence_count: number;
};

export type VerifiedSentimentDriver = {
  rank: number;
  headline_or_post_summary: string;
  lang: "VI" | "EN";
  platform_or_domain: string;
  tier: "TIER_1" | "TIER_2" | "TIER_3" | "TIER_4";
  freshness: "BREAKING_6H" | "INTRADAY_24H" | "SHORT_3D" | "WEEKLY_7D" | "STALE";
  effective_weight: number;
  polarity: "POSITIVE" | "NEGATIVE" | "NEUTRAL";
  evidence_quote: string;
  url: string | null;
};

export type MarketSentimentResponse = {
  module: string;
  entity: {
    ticker: string;
    resolved_name: string;
    exchange: string;
    timestamp_utc: string;
  };
  sentiment_summary: {
    composite_score: number;
    text_score?: number | null;
    quant_score?: number | null;
    pillar_weights?: Record<string, number> | null;
    institutional_vs_retail_spread: number;
    label: "STRONG_BULLISH" | "BULLISH" | "NEUTRAL" | "BEARISH" | "STRONG_BEARISH";
    confidence: number;
    market_regime: string;
    momentum_24h: string;
  };
  benchmark_standards: {
    fear_greed_index: number | null;
    fear_greed_zone: string;
    vietstock_psychology_state: string;
    smart_money_confidence: number | null;
    dumb_money_confidence: number | null;
    smart_dumb_spread: number | null;
    contrarian_signal: string;
    buzz_velocity: string;
    insider_signal: string;
    uncertainty_and_litigation_score: number;
    nlp_telemetry: {
      tfidf_corpus_weighting: boolean;
      corpus_doc_count: number;
      tetlock_media_pessimism: number;
      consensus_polarization: number;
      source_diversity_entropy: number;
    } | null;
    quantitative_telemetry: {
      ohlcv: {
        available: boolean;
        bars_count: number;
        source_name: string | null;
        source_url: string | null;
        as_of: string | null;
        last_close: number | null;
        return_1d_pct: number | null;
        return_20d_pct: number | null;
        rsi_14: number | null;
        mfi_14: number | null;
        cmf_21: number | null;
      } | null;
      market_proxies: {
        available: boolean;
        vix_term_ratio: number | null;
        vix_term_regime: string | null;
        cboe_put_call_ratio: number | null;
        dix_pct: number | null;
        social_s_score: number | null;
        insider_mspr: number | null;
        arms_trin: number | null;
        aaii_bull_bear_spread: number | null;
        source_note: string | null;
      } | null;
      fundamentals: {
        pe: number | null;
        pb: number | null;
        roe: number | null;
        market_cap: number | null;
        source: string | null;
        source_url: string | null;
        as_of: string | null;
      } | null;
    } | null;
    seven_pillar_checklist: SentimentPillarItem[];
  };
  network_telemetry: {
    official_and_media: {
      weighted_score: number;
      hit_count: number;
      key_catalysts: string[];
    };
    vn_social_ecosystem: {
      weighted_score: number;
      platforms_covered: string[];
      hit_count: number;
      crowd_narrative_vi: string;
      trending_slang_vi: string[];
    };
    global_social_ecosystem: {
      weighted_score: number;
      platforms_covered: string[];
      hit_count: number;
      crowd_narrative_en: string;
      trending_tags_en: string[];
    };
  };
  social_integrity_audit: {
    manipulation_or_seeding_detected: boolean;
    unverified_rumor_count: number;
    audit_notes: string;
  };
  top_verified_drivers: VerifiedSentimentDriver[];
  risk_circuit_breakers: string[];
  institutional_flows?: Record<string, unknown> | null;
  market_breadth?: Record<string, unknown> | null;
  analyst_consensus?: Record<string, unknown> | null;
  corporate_events?: Record<string, unknown> | null;
  historical_s_score?: {
    available: boolean;
    history_days: number;
    s_score: number | null;
    sv_score: number | null;
    mean_20d_score?: number | null;
  } | null;
  llm_judge?: {
    narrative_summary: string;
    driver_alignment: string;
    contrarian_risk: string;
    score_adjustment: number;
  } | null;
};

export async function marketSentiment(
  req: MarketSentimentRequest,
  signal?: AbortSignal
): Promise<MarketSentimentResponse> {
  const resp = await fetch(`${API_BASE_URL}/v1/market/sentiment`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
    signal,
  });
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

// --- Spaces, Knowledge Graph & Hybrid Search (/v1/spaces*, /v1/hybrid) ------

export type SpaceItem = {
  id: string;
  name: string;
  slug: string;
  description: string;
  domains: string[];
  pinned_urls: string[];
  query_terms: string[];
  restrict: boolean;
  last_crawl_at: string | null;
  created_at: string | null;
};

export type SpaceListResponse = {
  items: SpaceItem[];
  storage: string;
};

export type SpaceCreateRequest = {
  name: string;
  description?: string;
  domains?: string[];
  pinned_urls?: string[];
  query_terms?: string[];
  restrict?: boolean;
};

export type SpaceCrawlResponse = {
  space: string;
  targets: number;
  pages: { url: string; status: string; title?: string; error?: string }[];
  capped: number;
};

export type GraphBuildStatus = {
  space: string;
  status: string;
  docs: number;
  nodes: number;
  edges: number;
  error: string | null;
  created_at: string | null;
};

export type GraphQueryResponse = {
  space: string;
  matched_labels: string[];
  nodes: { label: string; type: string; description: string }[];
  edges: { source: string; target: string; relation: string }[];
  answer: string | null;
};

export type HybridChunkItem = {
  chunk_id: string;
  url: string;
  title: string;
  domain: string;
  position: number;
  heading_path: string;
  content: string;
  score: number;
  dense: number | null;
  sparse: number;
};

export type HybridSearchResponse = {
  query: string;
  k: number;
  dense_used: boolean;
  chunks: HybridChunkItem[];
};

export async function listSpaces(signal?: AbortSignal): Promise<SpaceListResponse> {
  const resp = await fetch(`${API_BASE_URL}/v1/spaces`, { signal });
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export async function createSpace(
  req: SpaceCreateRequest,
  signal?: AbortSignal
): Promise<SpaceItem> {
  const resp = await fetch(`${API_BASE_URL}/v1/spaces`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
    signal,
  });
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export async function deleteSpace(
  slug: string,
  signal?: AbortSignal
): Promise<{ deleted: boolean; slug: string }> {
  const resp = await fetch(`${API_BASE_URL}/v1/spaces/${encodeURIComponent(slug)}`, {
    method: "DELETE",
    signal,
  });
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export async function crawlSpace(
  slug: string,
  maxPages = 20,
  signal?: AbortSignal
): Promise<SpaceCrawlResponse> {
  const resp = await fetch(
    `${API_BASE_URL}/v1/spaces/${encodeURIComponent(slug)}/crawl?max_pages=${encodeURIComponent(String(maxPages))}`,
    { method: "POST", signal }
  );
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export async function buildSpaceGraph(
  slug: string,
  signal?: AbortSignal
): Promise<GraphBuildStatus> {
  const resp = await fetch(
    `${API_BASE_URL}/v1/spaces/${encodeURIComponent(slug)}/graph/build`,
    { method: "POST", signal }
  );
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export async function spaceGraphStatus(
  slug: string,
  signal?: AbortSignal
): Promise<GraphBuildStatus> {
  const resp = await fetch(
    `${API_BASE_URL}/v1/spaces/${encodeURIComponent(slug)}/graph/status`,
    { signal }
  );
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export async function querySpaceGraph(
  slug: string,
  req: { query: string; answer?: boolean },
  signal?: AbortSignal
): Promise<GraphQueryResponse> {
  const resp = await fetch(
    `${API_BASE_URL}/v1/spaces/${encodeURIComponent(slug)}/graph/query`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req),
      signal,
    }
  );
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export async function hybridSearch(
  req: { query: string; k?: number; space?: string; dense?: boolean },
  signal?: AbortSignal
): Promise<HybridSearchResponse> {
  const resp = await fetch(`${API_BASE_URL}/v1/hybrid`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
    signal,
  });
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export type MarketNewsCategory =
  | "corporate_action"
  | "listing_status"
  | "insider_trade"
  | "regulatory"
  | "macro_policy"
  | "market_flow"
  | "general_finance";

export interface MarketNewsWireItem {
  title: string;
  url?: string | null;
  source_id: string;
  snippet?: string;
  published_at?: string | null;
  event_date?: string | null;
  symbols: string[];
  scope: "ticker" | "market" | "policy" | "sector" | "general";
  category: MarketNewsCategory;
  severity: "critical" | "high" | "medium" | "info";
  stable_id: string;
  fingerprint: string;
  date_status: "event_date" | "published_at" | "undated";
  duplicate_count: number;
  also_reported_by: string[];
  is_new: boolean;
}

export interface MarketNewsWireRequest {
  symbols?: string[];
  lang?: "auto" | "vi" | "en";
  categories?: MarketNewsCategory[];
  include_market_macro?: boolean;
  deduplicate?: boolean;
  max_age_days?: number;
  max_items?: number;
  seen_fingerprints?: string[];
  include_search_fallback?: boolean;
  timeout_ms?: number;
}

export interface MarketNewsWireResponse {
  items: MarketNewsWireItem[];
  new_count: number;
  total_raw: number;
  deduplicated_count: number;
  sources_used: string[];
  sources_failed: string[];
  warnings: string[];
  partial: boolean;
  new_fingerprints: string[];
  latency_ms: number;
}

export interface CorporateEventRecord {
  event_id: string;
  symbol?: string | null;
  exchange?: string | null;
  event_type: "corporate_action" | "dividend_calendar" | "listing" | "insider" | "regulatory";
  event_subtype: string;
  severity: "critical" | "high" | "medium" | "info";
  risk_level: number;
  title: string;
  detail?: string;
  event_date?: string | null;
  ex_date?: string | null;
  record_date?: string | null;
  payment_date?: string | null;
  days_until?: number | null;
  source_id: string;
  source_url?: string | null;
  is_share_changing: boolean;
  share_factor?: number | null;
  shares_delta?: number | null;
  is_dilution: boolean;
  cash_dividend_pct?: number | null;
  cash_per_share_vnd?: number | null;
  stock_ratio?: string | null;
  date_mismatch_warning?: string | null;
  is_new: boolean;
}

export interface ShareRatioExplanation {
  explained: boolean;
  observed_ratio: number;
  matched_factor?: number | null;
  residual_error_pct?: number | null;
  matched_events: Array<Record<string, unknown>>;
  explanation: string;
}

export interface MarketEventsRequest {
  symbols?: string[];
  include_corporate_actions?: boolean;
  include_calendar?: boolean;
  include_listing?: boolean;
  include_insider?: boolean;
  include_regulatory?: boolean;
  lookback_days?: number;
  lookahead_days?: number;
  reported_share_ratio?: number;
  observed_shares_high?: number;
  observed_shares_low?: number;
  ratio_tolerance?: number;
  seen_event_ids?: string[];
  timeout_ms?: number;
}

export interface MarketEventsResponse {
  symbols: string[];
  events: CorporateEventRecord[];
  upcoming_ex_dates: CorporateEventRecord[];
  dilution_alerts: CorporateEventRecord[];
  risk_alerts: CorporateEventRecord[];
  share_ratio_explanation?: ShareRatioExplanation | null;
  new_events_count: number;
  new_event_ids: string[];
  sources_used: string[];
  sources_failed: string[];
  warnings: string[];
  partial: boolean;
  latency_ms: number;
}

export async function fetchMarketNewsWire(
  req: MarketNewsWireRequest = {},
  signal?: AbortSignal
): Promise<MarketNewsWireResponse> {
  const resp = await fetch(`${API_BASE_URL}/v1/market/news-wire`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
    signal,
  });
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export async function fetchMarketEvents(
  req: MarketEventsRequest = {},
  signal?: AbortSignal
): Promise<MarketEventsResponse> {
  const resp = await fetch(`${API_BASE_URL}/v1/market/events`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
    signal,
  });
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

// ---------------------------------------------------------------------------
// v4.5.0 Trust & Insight layer (docs/34): claim verification + event timeline
// ---------------------------------------------------------------------------

export type VerifyStance = "supports" | "refutes" | "mentions";
export type VerifyVerdict = "supported" | "refuted" | "contested" | "insufficient_evidence";

export type VerifyEvidence = {
  n: number;
  url: string;
  title: string;
  domain: string;
  domain_tier: string;
  stance: VerifyStance;
  relevance: number;
  quote: string;
  numeric_match?: number | null;
  refute_cue?: string | null;
  polarity_flip: boolean;
  hearsay?: boolean;
  judge: "lexical" | "llm";
  published_at?: string | null;
  fetched_at?: string | null;
  weight: number;
};

export type VerifyResponse = {
  claim: string;
  verdict: VerifyVerdict;
  confidence: number;
  single_source: boolean;
  independent_domains: number;
  support_weight: number;
  refute_weight: number;
  counts: Record<string, number>;
  judge: "lexical" | "llm";
  evidence: VerifyEvidence[];
  sources_considered: number;
  chunks_considered: number;
  query_id?: string | null;
  complete: boolean;
  latency_ms?: number | null;
  method: string;
};

export async function verifyClaim(
  claim: string,
  opts: { useLlm?: boolean; freshnessDays?: number | null; lang?: string | null } = {},
  signal?: AbortSignal
): Promise<VerifyResponse> {
  const body: Record<string, unknown> = { claim, use_llm: Boolean(opts.useLlm) };
  if (opts.freshnessDays) body.freshness_days = opts.freshnessDays;
  if (opts.lang) body.lang = opts.lang;
  const resp = await fetch(`${API_BASE_URL}/v1/verify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}

export type TimelineSource = {
  url: string;
  title: string;
  domain: string;
  domain_tier: string;
  published_at: string;
};

export type TimelineEvent = {
  date: string;
  first_reported_at: string;
  last_update_at: string;
  title: string;
  summary: string;
  sources_count: number;
  best_domain_tier: string;
  sources: TimelineSource[];
};

export type TimelineResponse = {
  topic: string;
  events: TimelineEvent[];
  coverage: {
    distinct_domains: number;
    by_domain_tier: Record<string, number>;
    by_country: Record<string, number>;
    volume_by_day: { date: string; count: number }[];
  };
  window: { start: string; end: string; days: number };
  results_considered: number;
  dated_results: number;
  undated_dropped: number;
  out_of_window_dropped: number;
  events_total: number;
  complete: boolean;
  cached?: boolean;
  query_id?: string | null;
};

export async function eventTimeline(
  topic: string,
  opts: { days?: number; lang?: string | null; maxEvents?: number } = {},
  signal?: AbortSignal
): Promise<TimelineResponse> {
  const body: Record<string, unknown> = { topic, days: opts.days ?? 30 };
  if (opts.lang) body.lang = opts.lang;
  if (opts.maxEvents) body.max_events = opts.maxEvents;
  const resp = await fetch(`${API_BASE_URL}/v1/timeline`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!resp.ok) throw await parseError(resp);
  return resp.json();
}
