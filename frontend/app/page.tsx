"use client";

import { ReactNode, useCallback, useEffect, useRef, useState } from "react";

import NewsPanel from "../components/NewsPanel";
import ResearchPanel from "../components/ResearchPanel";
import ComparePanel from "../components/ComparePanel";
import { dictationIsSupported, mergeTranscripts, startDictation, type DictationHandle } from "../lib/dictation";
import AnswerMarkdown from "../components/AnswerMarkdown";
import {
  getPreferredEngine,
  TtsEngine,
  TtsHandle,
  TtsVoiceMode,
  ttsIsSupported,
  ttsOnVoicesChanged,
  ttsSpeak,
  ttsSpeakRemote,
  ttsStop,
  ttsVietnameseVoiceMode,
} from "../lib/tts";
import {
  API_BASE_URL,
  ActiveLLMConfig,
  AnswerMeta,
  BlogPostDetail,
  BlogPostSummary,
  Citation,
  CouncilVerdict,
  DomainProfileInfo,
  EngineDone,
  EngineInfo,
  LLMOverridePayload,
  LLMProviderPresetInfo,
  LLMTestResult,
  safeHref,
  SearchHit,
  SourceItem,
  SuperAgentTicket,
  SuperAgentWatchdogStatus,
  createSuperAgentTicket,
  getDigest,
  getDomains,
  getLLMProviders,
  getSuperAgentWatchdogStatus,
  listDigests,
  listEngines,
  listSuperAgentTickets,
  resetLLMConfig,
  resolveSuperAgentEscalation,
  search,
  streamAnswer,
  testLLMConnection,
  triggerScan,
  ttsCapabilities,
  updateCouncilEndpoints,
  updateLLMConfig,
} from "../lib/api";

type Mode = "answer" | "raw";
type AppTab = "search" | "news" | "research" | "compare" | "blog" | "superagent" | "llm";

const CATEGORY_CHIPS = [
  { id: "auto", label: "⚡ Tự động (Universal Domain Intelligence)" },
  { id: "financial", label: "📈 Tài chính & BCTC (Flagship Pilot)" },
  { id: "news", label: "📰 Tin tức & Địa chính trị" },
  { id: "developer", label: "💻 Công nghệ, AI & Kỹ thuật" },
  { id: "academic", label: "🔬 Học thuật & Y sinh Lâm sàng" },
  { id: "fengshui", label: "🧭 Phong thủy & Bát trạch" },
  { id: "reference", label: "📚 Vĩ mô & Pháp lý" },
  { id: "social", label: "💬 Thảo luận đa chiều" },
];

const TOPIC_CHIPS = [
  { id: "all", label: "Tất cả" },
  { id: "ai_coding", label: "AI Coding & LLM", badge: "badge-ai" },
  { id: "ai_agents", label: "AI Agents & MCP", badge: "badge-ai" },
  { id: "viet_finance", label: "Chứng Khoán VN & VN-Index", badge: "badge-finance" },
  { id: "macro_business", label: "Kinh Tế Vĩ Mô", badge: "badge-news" },
  { id: "fengshui_lifestyle", label: "Phong Thủy Ứng Dụng", badge: "badge-fengshui" },
  { id: "health_wellness", label: "Y Học & Sức Khỏe", badge: "badge-health" },
];

export default function HomePage() {
  const [activeTab, setActiveTab] = useState<AppTab>("search");

  // Search state
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<Mode>("answer");
  const [selectedCategory, setSelectedCategory] = useState<string>("auto");
  // docs/19: global search — language selector (auto = let the backend detect)
  // and deep mode (fan out to ALL engines, ignoring the category filter).
  const [searchLang, setSearchLang] = useState<string>("auto");
  const [deepSearch, setDeepSearch] = useState<boolean>(false);
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState("");
  const [engines, setEngines] = useState<EngineDone[]>([]);
  const [sources, setSources] = useState<SourceItem[]>([]);
  const [answerMd, setAnswerMd] = useState("");
  const [citations, setCitations] = useState<Citation[]>([]);
  const [councilVerdict, setCouncilVerdict] = useState<CouncilVerdict | null>(null);
  const [councilMode, setCouncilMode] = useState<boolean>(false);
  const [councilEndpointsText, setCouncilEndpointsText] = useState<string>(
    "openrouter:openrouter/free,pollinations:openai,groq:llama-3.3-70b-versatile,gemini:gemini-2.0-flash"
  );
  const [domains, setDomains] = useState<DomainProfileInfo[]>([]);
  const [selectedDomain, setSelectedDomain] = useState<string>("");
  const [meta, setMeta] = useState<AnswerMeta | null>(null);
  const [error, setError] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [searchInfo, setSearchInfo] = useState("");
  const [engineOptions, setEngineOptions] = useState<EngineInfo[]>([]);
  const [selectedEngines, setSelectedEngines] = useState<string[]>([]);
  const abortRef = useRef<AbortController | null>(null);

  // Vietnamese TTS for the synthesized answer (Web Speech API — lib/tts.ts).
  const answerTtsRef = useRef<TtsHandle | null>(null);
  const [answerSpeaking, setAnswerSpeaking] = useState(false);
  const [ttsReady, setTtsReady] = useState(false);

  // Vietnamese dictation (docs/18 A1): speech -> query input.
  const [micReady, setMicReady] = useState(false);
  const [listening, setListening] = useState(false);
  const micHandleRef = useRef<DictationHandle | null>(null);
  const micBaseRef = useRef("");
  const [voiceMode, setVoiceMode] = useState<TtsVoiceMode>("none");
  const [ttsEngine, setTtsEngine] = useState<TtsEngine>("webspeech");
  const [vsEnabled, setVsEnabled] = useState(false);

  // Blog / Daily Briefing state
  const [blogTopic, setBlogTopic] = useState<string>("all");
  const [blogPosts, setBlogPosts] = useState<BlogPostSummary[]>([]);
  const [blogLoading, setBlogLoading] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [selectedPost, setSelectedPost] = useState<BlogPostDetail | null>(null);
  const [modalLoading, setModalLoading] = useState(false);

  // LLM Configuration & External Integration state
  const [llmPresets, setLlmPresets] = useState<LLMProviderPresetInfo[]>([]);
  const [activeLlm, setActiveLlm] = useState<ActiveLLMConfig | null>(null);
  const [llmProvider, setLlmProvider] = useState<string>("openrouter");
  const [llmProtocol, setLlmProtocol] = useState<"openai" | "anthropic">("openai");
  const [llmBaseUrl, setLlmBaseUrl] = useState<string>("https://openrouter.ai/api/v1");
  const [llmApiKey, setLlmApiKey] = useState<string>("");
  const [llmQuickModel, setLlmQuickModel] = useState<string>("openrouter/free");
  const [llmReportModel, setLlmReportModel] = useState<string>("openrouter/free");
  const [llmFallbackProvider, setLlmFallbackProvider] = useState<string>("pollinations");
  const [llmHeadersJson, setLlmHeadersJson] = useState<string>("");
  const [llmSaving, setLlmSaving] = useState<boolean>(false);
  const [llmTesting, setLlmTesting] = useState<boolean>(false);
  const [llmStatusMsg, setLlmStatusMsg] = useState<string>("");
  const [llmTestResult, setLlmTestResult] = useState<LLMTestResult | null>(null);

  // Super-Agent state
  const [superagentTickets, setSuperagentTickets] = useState<SuperAgentTicket[]>([]);
  const [watchdogStatus, setWatchdogStatus] = useState<SuperAgentWatchdogStatus | null>(null);
  const [agentGoalTitle, setAgentGoalTitle] = useState("");
  const [agentGoalObjective, setAgentGoalObjective] = useState("");
  const [agentGoalDomain, setAgentGoalDomain] = useState("auto");
  const [agentExecutionMode, setAgentExecutionMode] = useState<"live" | "observe">("live");
  const [agentPriority, setAgentPriority] = useState<number>(3);
  const [agentLoading, setAgentLoading] = useState(false);
  const [selectedTicket, setSelectedTicket] = useState<SuperAgentTicket | null>(null);

  const loadSuperAgentData = useCallback(async (signal?: AbortSignal) => {
    try {
      const [tkts, wd] = await Promise.all([
        listSuperAgentTickets(undefined, signal),
        getSuperAgentWatchdogStatus(signal),
      ]);
      setSuperagentTickets(tkts);
      setWatchdogStatus(wd);
    } catch {
      // Ignore background errors
    }
  }, []);

  const loadLLMState = useCallback(async (signal?: AbortSignal) => {
    try {
      const data = await getLLMProviders(signal);
      setLlmPresets(data.providers);
      setActiveLlm(data.active);
      setLlmProvider(data.active.provider);
      setLlmProtocol(
        data.active.protocol === "anthropic" ? "anthropic" : "openai"
      );
      setLlmBaseUrl(data.active.base_url);
      setLlmQuickModel(data.active.quick_model);
      setLlmReportModel(data.active.report_model);
      setLlmFallbackProvider(data.active.fallback_provider || "");
      if (data.active.council_endpoints && data.active.council_endpoints.length > 0) {
        setCouncilEndpointsText(data.active.council_endpoints.join(","));
      }
      if (data.active.council_enabled) {
        setCouncilMode(true);
      }
    } catch {
      // Ignore on offline boot
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    listEngines(controller.signal)
      .then((items) =>
        setEngineOptions(items.filter((e) => e.enabled && e.adapter_ready))
      )
      .catch(() => setEngineOptions([]));
    getDomains(controller.signal)
      .then((resp) => setDomains(resp.domains))
      .catch(() => setDomains([]));
    loadLLMState(controller.signal);
    return () => controller.abort();
  }, [loadLLMState]);

  const handleSelectPreset = (presetId: string) => {
    setLlmProvider(presetId);
    setLlmTestResult(null);
    setLlmStatusMsg("");
    const p = llmPresets.find((item) => item.id === presetId);
    if (p) {
      setLlmProtocol(p.protocol === "anthropic" ? "anthropic" : "openai");
      setLlmBaseUrl(p.base_url);
      setLlmQuickModel(p.default_quick_model);
      setLlmReportModel(p.default_report_model);
    }
  };

  const buildOverridePayload = (): LLMOverridePayload => {
    let parsedHeaders: Record<string, string> | undefined;
    if (llmHeadersJson.trim()) {
      try {
        parsedHeaders = JSON.parse(llmHeadersJson.trim());
      } catch {
        throw new Error("Custom Headers phải là chuỗi JSON hợp lệ (VD: {\"X-Org\":\"abc\"})");
      }
    }
    const payload: LLMOverridePayload = {
      provider: llmProvider,
      protocol: llmProtocol,
      base_url: llmBaseUrl.trim() || undefined,
      quick_model: llmQuickModel.trim() || undefined,
      report_model: llmReportModel.trim() || undefined,
      fallback_provider: llmFallbackProvider,
      extra_headers: parsedHeaders,
    };
    if (llmApiKey.trim()) {
      payload.api_key = llmApiKey.trim();
    }
    return payload;
  };

  const handleSaveLLMConfig = async () => {
    setLlmSaving(true);
    setLlmStatusMsg("");
    setLlmTestResult(null);
    try {
      const payload = buildOverridePayload();
      const updated = await updateLLMConfig(payload);
      setActiveLlm(updated);
      setLlmApiKey("");
      setLlmStatusMsg(
        `✅ Đã lưu cấu hình nóng: ${updated.provider_name} (Model: ${updated.quick_model})`
      );
    } catch (err: unknown) {
      setLlmStatusMsg(
        `❌ Lỗi lưu cấu hình: ${err instanceof Error ? err.message : String(err)}`
      );
    } finally {
      setLlmSaving(false);
    }
  };

  const handleResetLLMConfig = async () => {
    setLlmSaving(true);
    setLlmStatusMsg("");
    setLlmTestResult(null);
    try {
      const updated = await resetLLMConfig();
      setActiveLlm(updated);
      await loadLLMState();
      setLlmStatusMsg("🔄 Đã khôi phục cấu hình LLM mặc định từ biến môi trường (.env)");
    } catch (err: unknown) {
      setLlmStatusMsg(
        `❌ Lỗi khôi phục: ${err instanceof Error ? err.message : String(err)}`
      );
    } finally {
      setLlmSaving(false);
    }
  };

  const handleTestLLM = async () => {
    setLlmTesting(true);
    setLlmTestResult(null);
    try {
      const payload = buildOverridePayload();
      const res = await testLLMConnection(payload);
      setLlmTestResult(res);
    } catch (err: unknown) {
      setLlmTestResult({
        ok: false,
        provider: llmProvider,
        model: llmQuickModel,
        base_url: llmBaseUrl,
        error: err instanceof Error ? err.message : String(err),
        latency_ms: 0,
      });
    } finally {
      setLlmTesting(false);
    }
  };

  const loadBlogPosts = useCallback(async (topicId?: string) => {
    setBlogLoading(true);
    try {
      const topicFilter = topicId && topicId !== "all" ? topicId : undefined;
      const data = await listDigests(topicFilter);
      setBlogPosts(data);
    } catch {
      setBlogPosts([]);
    } finally {
      setBlogLoading(false);
    }
  }, []);

  useEffect(() => {
    if (activeTab === "blog") {
      loadBlogPosts(blogTopic);
    }
    if (activeTab === "superagent") {
      loadSuperAgentData();
    }
  }, [activeTab, blogTopic, loadBlogPosts, loadSuperAgentData]);

  // Web Speech API only exists in the browser — gate after mount (SSR safe).
  useEffect(() => {
    setTtsReady(ttsIsSupported());
    setMicReady(dictationIsSupported());
    setVoiceMode(ttsVietnameseVoiceMode());
    setTtsEngine(getPreferredEngine());
    ttsCapabilities()
      .then((c) => setVsEnabled(Boolean(c.voicestudio.enabled)))
      .catch(() => setVsEnabled(false));
    const offVoices = ttsOnVoicesChanged(() => setVoiceMode(ttsVietnameseVoiceMode()));
    return () => {
      offVoices();
      ttsStop();
      answerTtsRef.current?.cancel();
      micHandleRef.current?.stop();
    };
  }, []);

  const toggleMic = () => {
    if (listening) {
      micHandleRef.current?.stop();
      micHandleRef.current = null;
      setListening(false);
      return;
    }
    micBaseRef.current = query;
    const handle = startDictation({
      onUpdate: (committed, interim) => {
        setQuery(mergeTranscripts(micBaseRef.current, committed, interim));
      },
      onEnd: () => setListening(false),
    });
    if (handle) {
      micHandleRef.current = handle;
      setListening(true);
    }
  };

  const effectiveTtsEngine: TtsEngine =
    ttsEngine === "voicestudio" && !vsEnabled ? "webspeech" : ttsEngine;

  // Privacy wording matching the engine/voice actually in use (local OS voice
  // keeps the spoken text on-device; an online voice sends it to the vendor;
  // VoiceStudio sends it to the configured internal server).
  const ttsPrivacyNote =
    effectiveTtsEngine === "voicestudio"
      ? "Riêng tư: nội dung đọc được gửi tới máy chủ VoiceStudio nội bộ (VOICESTUDIO_URL ở backend), không ra Internet."
      : voiceMode === "local"
        ? "Riêng tư: đang dùng giọng nội bộ của hệ điều hành — nội dung đọc không rời khỏi máy."
        : voiceMode === "online"
          ? "Riêng tư: đang dùng giọng online (Edge/Microsoft) — nội dung đọc được gửi từ trình duyệt tới máy chủ TTS."
          : "Chưa có giọng tiếng Việt trên máy — sẽ đọc bằng giọng mặc định.";

  // Leaving the search tab unmounts the answer controls — speech must not
  // continue uncontrolled (QA/SA audits).
  useEffect(() => {
    if (activeTab !== "search") {
      answerTtsRef.current?.cancel();
      answerTtsRef.current = null;
      ttsStop();
      setAnswerSpeaking(false);
    }
  }, [activeTab]);

  const readAnswerAloud = () => {
    if (answerSpeaking) {
      answerTtsRef.current?.cancel();
      answerTtsRef.current = null;
      ttsStop();
      setAnswerSpeaking(false);
      return;
    }
    const cbs = {
      onStart: () => setAnswerSpeaking(true),
      onEnd: () => setAnswerSpeaking(false),
      onError: () => setAnswerSpeaking(false),
      // docs/17 §2: a failed VoiceStudio chain falls back to the browser
      // engine instead of leaving the user in silence.
      fallbackToWebSpeech: true,
    };
    answerTtsRef.current =
      effectiveTtsEngine === "voicestudio" ? ttsSpeakRemote(answerMd, cbs) : ttsSpeak(answerMd, cbs);
  };

  const handleCreateAgentTicket = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!agentGoalTitle.trim() || !agentGoalObjective.trim()) return;
    setAgentLoading(true);
    try {
      await createSuperAgentTicket({
        title: agentGoalTitle.trim(),
        objective: agentGoalObjective.trim(),
        domain: agentGoalDomain,
        priority: agentPriority,
        execution_mode: agentExecutionMode,
        run_immediately: true,
      });
      setAgentGoalTitle("");
      setAgentGoalObjective("");
      await loadSuperAgentData();
    } catch (err: unknown) {
      alert("Lỗi tạo Ticket: " + (err instanceof Error ? err.message : String(err)));
    } finally {
      setAgentLoading(false);
    }
  };

  const handleResolveEscalation = async (
    ticketId: string,
    escalationId: string,
    option: string
  ) => {
    try {
      await resolveSuperAgentEscalation(ticketId, escalationId, option);
      await loadSuperAgentData();
    } catch (err: unknown) {
      alert("Lỗi phê duyệt: " + (err instanceof Error ? err.message : String(err)));
    }
  };

  const handleTriggerScan = async () => {
    setScanning(true);
    try {
      const targetTopic = blogTopic !== "all" ? blogTopic : undefined;
      await triggerScan(targetTopic);
      await loadBlogPosts(blogTopic);
    } catch (err: unknown) {
      alert("Lỗi khi quét tin: " + (err instanceof Error ? err.message : String(err)));
    } finally {
      setScanning(false);
    }
  };

  const handleReadPost = async (slugOrId: string) => {
    setModalLoading(true);
    try {
      const detail = await getDigest(slugOrId);
      setSelectedPost(detail);
    } catch (err: unknown) {
      alert("Không thể tải bài viết: " + (err instanceof Error ? err.message : String(err)));
    } finally {
      setModalLoading(false);
    }
  };

  const toggleEngine = (id: string) => {
    setSelectedEngines((prev) =>
      prev.includes(id) ? prev.filter((e) => e !== id) : [...prev, id]
    );
  };

  const reset = useCallback(() => {
    setStage("");
    setEngines([]);
    setSources([]);
    setAnswerMd("");
    setCitations([]);
    setCouncilVerdict(null);
    setMeta(null);
    setError("");
    setHits([]);
    setSearchInfo("");
    // A cleared answer must not keep being read aloud.
    answerTtsRef.current?.cancel();
    answerTtsRef.current = null;
    ttsStop();
    setAnswerSpeaking(false);
  }, []);

  const handleSaveCouncilEndpoints = async () => {
    const specs = councilEndpointsText
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    if (specs.length === 0) return;
    try {
      const res = await updateCouncilEndpoints(specs);
      setLlmStatusMsg(
        `✅ Đã cập nhật Hội đồng Phán quyết (${res.count} endpoints): ${res.council_endpoints.join(", ")}`
      );
      await loadLLMState();
    } catch (err: unknown) {
      setLlmStatusMsg(
        `❌ Lỗi cập nhật Hội đồng: ${err instanceof Error ? err.message : String(err)}`
      );
    }
  };

  const run = useCallback(async () => {
    const q = query.trim();
    if (!q || busy) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    reset();
    setBusy(true);
    const categoriesParam =
      selectedCategory === "auto" ? undefined : ["general", selectedCategory];
    const councilEndpointsParam = councilMode
      ? councilEndpointsText
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean)
      : undefined;
    try {
      if (mode === "raw") {
        const resp = await search(q, {
          signal: controller.signal,
          engines: selectedEngines.length ? selectedEngines : undefined,
          categories: categoriesParam,
          lang: searchLang === "auto" ? undefined : searchLang,
          deep: deepSearch,
        });
        setHits(resp.results);
        const incomplete = resp.complete === false;
        const emptyNote =
          resp.results.length === 0
            ? incomplete
              ? " · chưa đủ dữ liệu để kết luận là không có kết quả"
              : " · không có kết quả (mọi engine đã trả lời)"
            : "";
        setSearchInfo(
          `${resp.results.length} kết quả · ${resp.latency_ms} ms · engines: ` +
            `${resp.engines_used.join(", ") || "-"} · ${resp.cached ? "cached" : "fresh"}` +
            (incomplete ? " · chưa đầy đủ" : "") +
            emptyNote
        );
      } else {
        await streamAnswer(
          q,
          {
            onStatus: (stageName, detail) => setStage(`${stageName}: ${detail}`),
            onEngineDone: (info) => setEngines((prev) => [...prev, info]),
            onSources: (items) => setSources(items),
            onToken: (text) => setAnswerMd((prev) => prev + text),
            onCitations: (items) => setCitations(items),
            onCouncil: (verdict) => setCouncilVerdict(verdict),
            onDone: (m) => setMeta(m),
            onError: (code, message) => setError(`[${code}] ${message}`),
          },
          controller.signal,
          selectedEngines.length ? selectedEngines : undefined,
          categoriesParam,
          undefined,
          councilMode,
          councilEndpointsParam,
          selectedDomain || undefined,
          searchLang === "auto" ? undefined : searchLang,
          deepSearch
        );
      }
    } catch (err: unknown) {
      if (err instanceof Error && err.name === "AbortError") return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [
    busy,
    councilEndpointsText,
    councilMode,
    mode,
    query,
    reset,
    selectedCategory,
    selectedDomain,
    selectedEngines,
    searchLang,
    deepSearch,
  ]);

  const citationByUrl = new Map(citations.map((c) => [c.n, c]));
  const selectedPresetObj = llmPresets.find((p) => p.id === llmProvider);

  return (
    <main className="shell">
      <header>
        <h1>MetaSearch</h1>
        <p className="tagline">
          Hệ thống tìm kiếm tổng hợp đa tầng, Thẩm định Pháp y BCTC & Deep Research thông minh
        </p>

        {/* Tab navigation */}
        <nav className="nav-tabs">
          <button
            className={`nav-tab ${activeTab === "search" ? "active" : ""}`}
            onClick={() => setActiveTab("search")}
          >
            🔍 Tìm Kiếm & Hỏi Đáp (Live)
          </button>
          <button
            className={`nav-tab ${activeTab === "news" ? "active" : ""}`}
            onClick={() => setActiveTab("news")}
          >
            🗞️ Tin Tức (Kinh tế · Tài chính · Công nghệ)
          </button>
          <button
            className={`nav-tab ${activeTab === "research" ? "active" : ""}`}
            onClick={() => setActiveTab("research")}
          >
            🔬 Deep Research (Tự trị)
          </button>
          <button
            className={`nav-tab ${activeTab === "compare" ? "active" : ""}`}
            onClick={() => setActiveTab("compare")}
          >
            ⚖️ So sánh mã
          </button>
          <button
            className={`nav-tab ${activeTab === "blog" ? "active" : ""}`}
            onClick={() => setActiveTab("blog")}
          >
            📰 Bản Tin & Blog Chuyên Sâu
          </button>
          <button
            className={`nav-tab ${activeTab === "superagent" ? "active" : ""}`}
            onClick={() => setActiveTab("superagent")}
          >
            ⚡ Super-Agent (DeepSeek Proactive)
          </button>
          <button
            className={`nav-tab ${activeTab === "llm" ? "active" : ""}`}
            onClick={() => setActiveTab("llm")}
          >
            🤖 Cấu Hình LLM (OpenRouter Free / BYO-LLM)
          </button>
        </nav>
      </header>

      {/* --- TAB 1B: NEWS FEED + VIETNAMESE TTS --- */}
      {activeTab === "news" && <NewsPanel />}
      {activeTab === "compare" && <ComparePanel />}
      {activeTab === "research" && <ResearchPanel />}

      {/* --- TAB 1: SEARCH & ANSWER --- */}
      {activeTab === "search" && (
        <>
          <section className="card">
            <form
              onSubmit={(e) => {
                e.preventDefault();
                run();
              }}
            >
              <div className="search-row">
                <input
                  type="text"
                  placeholder="Nhập câu hỏi (VD: pháp y BCTC VCB, kỹ thuật FPT RSI 9, $NVDA, hướng nhà 1990...)"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  disabled={busy}
                />
                {micReady && (
                  <button
                    type="button"
                    className={`tts-btn ${listening ? "speaking" : ""}`}
                    onClick={toggleMic}
                    aria-pressed={listening}
                    aria-label={listening ? "Dừng ghi âm giọng nói" : "Nói truy vấn bằng giọng nói"}
                    title={listening ? "Đang nghe... bấm để dừng" : "Nói truy vấn bằng giọng nói (tiếng Việt). Lưu ý riêng tư: Chrome gửi audio lên máy chủ nhận diện giọng nói để chuyển thành chữ."}
                  >
                    {listening ? "⏹ Đang nghe" : "🎙️ Nói"}
                  </button>
                )}
                <button type="submit" disabled={busy || !query.trim()}>
                  {busy ? "Đang xử lý..." : "Tìm kiếm"}
                </button>
              </div>

              <div className="mode-toggle" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
                <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap" }}>
                  <label>
                    <input
                      type="radio"
                      name="mode"
                      value="answer"
                      checked={mode === "answer"}
                      onChange={() => setMode("answer")}
                      disabled={busy}
                    />
                    Câu trả lời tổng hợp (Answer + Citations)
                  </label>
                  <label>
                    <input
                      type="radio"
                      name="mode"
                      value="raw"
                      checked={mode === "raw"}
                      onChange={() => setMode("raw")}
                      disabled={busy}
                    />
                    Kết quả thô (Multi-Engine Hits)
                  </label>
                </div>

                <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap", alignItems: "center" }}>
                  <button
                    type="button"
                    className={`chip chip-toggle ${councilMode ? "on" : ""}`}
                    onClick={() => setCouncilMode((prev) => !prev)}
                    disabled={busy}
                    title="Gọi đồng thời nhiều endpoint LLM miễn phí, chấm chéo ẩn danh (Cross-Review) và cải tiến chính sách qua Dream-RSI"
                  >
                    ⚖️ Hội Đồng LLM + Dream-RSI: {councilMode ? "BẬT" : "TẮT"}
                  </button>

                  {activeLlm && (
                    <button
                      type="button"
                      className="chip chip-toggle on"
                      onClick={() => setActiveTab("llm")}
                      title="Bấm để đổi LLM Provider (OpenRouter Free, Pollinations, Gemini, Groq, NVIDIA NIM, SambaNova, Ollama, Custom...)"
                    >
                      🤖 LLM: {activeLlm.provider} ({activeLlm.quick_model})
                      {activeLlm.is_free ? " · FREE" : ""}
                    </button>
                  )}
                </div>
              </div>

              {domains.length > 0 && (
                <div className="category-bar" style={{ marginBottom: "0.4rem" }}>
                  <span className="engine-filter-label" style={{ marginRight: "0.4rem" }}>
                    Miền Chuyên Sâu (Universal Intelligence):
                  </span>
                  <button
                    type="button"
                    className={`chip chip-toggle ${selectedDomain === "" ? "on" : ""}`}
                    onClick={() => setSelectedDomain("")}
                    disabled={busy}
                  >
                    🧠 Tự nhận diện
                  </button>
                  {domains.map((dom) => (
                    <button
                      key={dom.id}
                      type="button"
                      className={`chip chip-toggle ${selectedDomain === dom.id ? "on" : ""}`}
                      onClick={() => setSelectedDomain(dom.id)}
                      disabled={busy}
                      title={`Rubric: ${dom.verification_rubric.join(" | ")}`}
                    >
                      {dom.maturity === "flagship_pilot" ? "⭐ " : ""}
                      {dom.name_vi}
                    </button>
                  ))}
                </div>
              )}

              <div className="category-bar">
                <span className="engine-filter-label" style={{ marginRight: "0.4rem" }}>
                  Ngôn ngữ:
                </span>
                {[
                  { id: "auto", label: "Tự động" },
                  { id: "vi", label: "🇻🇳 VI" },
                  { id: "en", label: "🇬🇧 EN" },
                ].map((l) => (
                  <button
                    key={l.id}
                    type="button"
                    className={`chip chip-toggle ${searchLang === l.id ? "on" : ""}`}
                    onClick={() => setSearchLang(l.id)}
                    disabled={busy}
                    title="Ngôn ngữ tìm kiếm: truyền tới searxng/ddg/wikipedia/google news/GDELT/OpenAlex (docs/19)"
                  >
                    {l.label}
                  </button>
                ))}
                <button
                  type="button"
                  className={`chip chip-toggle ${deepSearch ? "on" : ""}`}
                  onClick={() => setDeepSearch((v) => !v)}
                  disabled={busy}
                  title="Deep search: fan-out tới TẤT CẢ engine đã bật (bỏ lọc chuyên ngành), k tối đa — dùng cho câu hỏi cần mọi nguồn toàn cầu (docs/19)"
                >
                  🌐 Deep
                </button>
              </div>

              <div className="category-bar">
                <span className="engine-filter-label" style={{ marginRight: "0.4rem" }}>
                  Chuyên ngành:
                </span>
                {CATEGORY_CHIPS.map((cat) => (
                  <button
                    key={cat.id}
                    type="button"
                    className={`chip chip-toggle ${selectedCategory === cat.id ? "on" : ""}`}
                    onClick={() => setSelectedCategory(cat.id)}
                    disabled={busy}
                  >
                    {cat.label}
                  </button>
                ))}
              </div>

              {engineOptions.length > 0 && (
                <div className="engine-filter">
                  <span className="engine-filter-label">
                    Engines ({engineOptions.length} sẵn sàng):
                  </span>
                  <div className="engine-chips">
                    {engineOptions.map((e) => {
                      const on = selectedEngines.includes(e.id);
                      return (
                        <button
                          key={e.id}
                          type="button"
                          className={`chip chip-toggle ${on ? "on" : ""}`}
                          onClick={() => toggleEngine(e.id)}
                          disabled={busy}
                          title={`${e.name || e.id} (${e.category || "general"})`}
                        >
                          {e.id}
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}
            </form>
          </section>

          {engines.length > 0 && (
            <div className="engines">
              {engines.map((eng, idx) => (
                <span key={`${eng.engine}-${idx}`} className="chip">
                  ✓ {eng.engine}: {eng.results} hits ({eng.ms} ms)
                </span>
              ))}
            </div>
          )}

          {busy && (
            <div className="card status">
              <div className="spinner" />
              <span>{stage || "Đang kết nối hệ thống tìm kiếm..."}</span>
            </div>
          )}

          {error && <div className="card error">{error}</div>}

          <div className="main-grid">
            <section className="results">
              {mode === "answer" && answerMd && (
                <article className="card answer">
                  <header className="answer-header">
                    <h2>Câu trả lời</h2>
                    <span className="answer-header-actions">
                      {meta && (
                        <span className="answer-meta">
                          {meta.latency_ms} ms · {meta.model}
                          {meta.cached ? " · cached" : ""}
                        </span>
                      )}
                      {(ttsReady || (vsEnabled && effectiveTtsEngine === "voicestudio")) && (
                        <button
                          type="button"
                          className={`tts-btn ${answerSpeaking ? "speaking" : ""}`}
                          onClick={readAnswerAloud}
                          aria-label={answerSpeaking ? "Dừng đọc câu trả lời" : "Đọc câu trả lời"}
                          title={`Đọc câu trả lời bằng giọng đọc tiếng Việt. ${ttsPrivacyNote}`}
                        >
                          {answerSpeaking ? "⏹ Dừng đọc" : "🔊 Đọc câu trả lời"}
                        </button>
                      )}
                    </span>
                  </header>
                  <AnswerMarkdown text={answerMd} citationByUrl={citationByUrl} />
                  {councilVerdict && (
                    <div
                      style={{
                        marginTop: "1rem",
                        padding: "0.9rem",
                        borderRadius: "8px",
                        border: "1px solid var(--border)",
                        background: "rgba(255,255,255,0.02)",
                        fontSize: "0.85rem",
                      }}
                    >
                      <h3 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>
                        ⚖️ Biên Bản Hội Đồng Phán Quyết (Multi-Endpoint Cross-Review & Dream-RSI)
                      </h3>
                      <p style={{ margin: "0 0 0.6rem", color: "var(--muted)" }}>
                        Miền thẩm định: <strong>{councilVerdict.domain}</strong> · Chủ tịch Hội đồng:{" "}
                        <code>{councilVerdict.chairman_model}</code> · Điểm đồng thuận:{" "}
                        <strong>{(councilVerdict.consensus_agreement_score * 100).toFixed(1)}%</strong> ·{" "}
                        {councilVerdict.total_latency_ms} ms
                      </p>
                      <div style={{ display: "grid", gap: "0.4rem" }}>
                        {councilVerdict.panelists.map((p) => (
                          <div
                            key={p.candidate_id}
                            style={{
                              padding: "0.45rem 0.65rem",
                              borderRadius: "6px",
                              border: "1px solid var(--border)",
                            }}
                          >
                            <strong>{p.candidate_id}</strong> ({p.provider}:<code>{p.model}</code>) —{" "}
                            {p.ok ? (
                              <>
                                Tổng điểm: <strong>{(p.composite_score * 100).toFixed(1)}%</strong>{" "}
                                (Định lượng: {(p.deterministic_score * 100).toFixed(0)}% · Peer Review:{" "}
                                {(p.peer_score * 100).toFixed(0)}%) · {p.latency_ms} ms
                              </>
                            ) : (
                              <span style={{ color: "#f87171" }}>Bỏ qua ({p.error})</span>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                  {citations.length > 0 && (
                    <div className="citations">
                      <h3>Trích dẫn nguồn ({citations.length})</h3>
                      <ol>
                        {citations.map((c) => (
                          <li key={c.n}>
                            <a href={safeHref(c.url)} target="_blank" rel="noreferrer">
                              [{c.n}] {c.title || c.url}
                            </a>
                          </li>
                        ))}
                      </ol>
                    </div>
                  )}
                </article>
              )}

              {mode === "raw" && hits.length > 0 && (
                <div className="hits">
                  {hits.map((h) => (
                    <article className="card hit" key={h.url}>
                      <a className="hit-title" href={safeHref(h.url)} target="_blank" rel="noreferrer">
                        {h.rank}. {h.title || h.url}
                      </a>
                      <div className="hit-url">{h.url}</div>
                      {h.snippet && <p className="hit-snippet">{h.snippet}</p>}
                      <div className="hit-meta">
                        score {h.score.toFixed(3)} · consensus {h.consensus} ·{" "}
                        {(h.engines ?? h.found_by).join(", ")}
                      </div>
                    </article>
                  ))}
                  {searchInfo && <p className="hit-info">{searchInfo}</p>}
                </div>
              )}
            </section>

            {sources.length > 0 && (
              <aside className="card sources">
                <h3>Nguồn trích xuất ({sources.length})</h3>
                <ul>
                  {sources.map((s, i) => (
                    <li key={`${s.url}-${i}`}>
                      <a href={safeHref(s.url)} target="_blank" rel="noreferrer" title={s.url}>
                        {s.title || s.url}
                      </a>
                      <div className="source-domain">{s.domain}</div>
                    </li>
                  ))}
                </ul>
              </aside>
            )}
          </div>
        </>
      )}

      {/* --- TAB 2: BLOG & DAILY BRIEFINGS --- */}
      {activeTab === "blog" && (
        <section>
          <div className="blog-controls">
            <div className="blog-filters">
              {TOPIC_CHIPS.map((chip) => (
                <button
                  key={chip.id}
                  className={`topic-chip ${blogTopic === chip.id ? "active" : ""}`}
                  onClick={() => setBlogTopic(chip.id)}
                >
                  {chip.label}
                </button>
              ))}
            </div>

            <button
              className="scan-btn"
              onClick={handleTriggerScan}
              disabled={scanning}
            >
              {scanning ? "🔄 Đang quét & tổng hợp..." : "⚡ Quét & Tạo Tin Mới Ngay"}
            </button>
          </div>

          {blogLoading ? (
            <div className="card status">
              <div className="spinner" />
              <span>Đang tải danh sách bài viết phân tích...</span>
            </div>
          ) : blogPosts.length === 0 ? (
            <div className="card" style={{ textAlign: "center", padding: "3rem" }}>
              <p style={{ color: "var(--muted)", margin: "0 0 1rem" }}>
                Chưa có bản tin nào trong chuyên mục này.
              </p>
              <button className="scan-btn" onClick={handleTriggerScan} disabled={scanning}>
                ⚡ Bấm vào đây để quét tự động ngay
              </button>
            </div>
          ) : (
            <div className="blog-grid">
              {blogPosts.map((post) => {
                const badgeClass =
                  post.topic.includes("ai") ? "badge-ai" :
                  post.topic.includes("finance") ? "badge-finance" :
                  post.topic.includes("fengshui") ? "badge-fengshui" :
                  post.topic.includes("health") ? "badge-health" : "badge-news";

                return (
                  <article key={post.id} className="card blog-item">
                    <div className="blog-meta">
                      <span className={`badge ${badgeClass}`}>{post.topic}</span>
                      <span className="blog-date">
                        {post.created_at ? new Date(post.created_at).toLocaleDateString("vi-VN") : "Hôm nay"}
                      </span>
                    </div>

                    <h2
                      className="blog-title"
                      onClick={() => handleReadPost(post.slug)}
                    >
                      {post.title}
                    </h2>

                    {post.summary && <p className="blog-summary">{post.summary}</p>}

                    <div className="blog-footer">
                      <div className="tag-list">
                        {(post.tags || []).map((t) => (
                          <span key={t} className="tag-pill">
                            #{t}
                          </span>
                        ))}
                      </div>

                      <button
                        className="read-btn"
                        onClick={() => handleReadPost(post.slug)}
                      >
                        Đọc toàn văn →
                      </button>
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </section>
      )}

      {/* --- TAB 3: LLM CONFIGURATION & EXTERNAL SYSTEM INTEGRATION --- */}
      {activeTab === "llm" && (
        <section style={{ display: "grid", gap: "1.2rem" }}>
          {activeLlm && (
            <div className="card">
              <h2 style={{ margin: "0 0 0.5rem", fontSize: "1.1rem" }}>
                ⚡ Trạng Thái LLM Đang Kích Hoạt
              </h2>
              <p style={{ margin: 0, color: "var(--muted)", fontSize: "0.9rem" }}>
                Provider: <strong>{activeLlm.provider_name}</strong> · Quick Model:{" "}
                <code>{activeLlm.quick_model}</code> · Report Model:{" "}
                <code>{activeLlm.report_model}</code> · Endpoint:{" "}
                <code>{activeLlm.base_url || "N/A"}</code> · API Key:{" "}
                <strong>
                  {activeLlm.has_api_key
                    ? `Đã cấu hình (${activeLlm.masked_api_key})`
                    : activeLlm.requires_api_key
                    ? "Chưa nhập API Key"
                    : "Keyless (Không cần key)"}
                </strong>
                {activeLlm.fallback_provider
                  ? ` · Dự phòng: ${activeLlm.fallback_provider}`
                  : ""}
              </p>
            </div>
          )}

          <div className="card">
            <h2 style={{ margin: "0 0 0.6rem", fontSize: "1.15rem" }}>
              1. Chọn Nhà Cung Cấp LLM (20 Presets từ OpenRouter Free, nejib1/Free-LLM & awesome-freellm-apis)
            </h2>
            <p style={{ margin: "0 0 1rem", color: "var(--muted)", fontSize: "0.88rem" }}>
              Hỗ trợ sẵn{" "}
              <a
                href="https://openrouter.ai/openrouter/free"
                target="_blank"
                rel="noreferrer"
                style={{ color: "var(--accent)" }}
              >
                OpenRouter Free Models Router (openrouter/free)
              </a>
              , danh mục{" "}
              <a
                href="https://github.com/nejib1/Free-LLM"
                target="_blank"
                rel="noreferrer"
                style={{ color: "var(--accent)" }}
              >
                nejib1/Free-LLM (34+ Free LLM APIs)
              </a>{" "}
              và{" "}
              <a
                href="https://github.com/open-free-llm-api/awesome-freellm-apis"
                target="_blank"
                rel="noreferrer"
                style={{ color: "var(--accent)" }}
              >
                open-free-llm-api/awesome-freellm-apis (134+ Free LLM APIs)
              </a>{" "}
              (Pollinations Keyless, Google Gemini Free, Groq LPU Free, NVIDIA NIM, Cerebras,
              SambaNova, Mistral, Cohere, Chutes, SiliconFlow, Hyperbolic, Cloudflare Workers AI,
              GitHub Models, HuggingFace, Ollama Local) hoặc kết nối vào LLM nội bộ của bạn.
            </p>

            <div className="engine-chips" style={{ marginBottom: "1rem" }}>
              {llmPresets.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  className={`chip chip-toggle ${llmProvider === p.id ? "on" : ""}`}
                  onClick={() => handleSelectPreset(p.id)}
                >
                  {p.is_free ? "🟢 " : "🔵 "}
                  {p.id === "openrouter" ? "openrouter (openrouter/free)" : p.id}
                </button>
              ))}
            </div>

            {selectedPresetObj && (
              <div
                style={{
                  background: "rgba(255,255,255,0.03)",
                  border: "1px solid var(--border)",
                  borderRadius: "8px",
                  padding: "0.9rem",
                  marginBottom: "1rem",
                  fontSize: "0.88rem",
                }}
              >
                <div style={{ fontWeight: 600, marginBottom: "0.3rem" }}>
                  {selectedPresetObj.name}
                  {selectedPresetObj.is_free && (
                    <span className="badge badge-finance" style={{ marginLeft: "0.6rem" }}>
                      FREE ($0/M)
                    </span>
                  )}
                </div>
                <p style={{ margin: "0 0 0.5rem", color: "var(--muted)" }}>
                  {selectedPresetObj.description}
                </p>
                {selectedPresetObj.signup_url && (
                  <div>
                    🔗 Lấy API Key / Tài liệu:{" "}
                    <a
                      href={safeHref(selectedPresetObj.signup_url)}
                      target="_blank"
                      rel="noreferrer"
                      style={{ color: "var(--accent)" }}
                    >
                      {selectedPresetObj.signup_url}
                    </a>
                  </div>
                )}
              </div>
            )}

            <div style={{ display: "grid", gap: "0.8rem", gridTemplateColumns: "1fr 1fr" }}>
              <div>
                <label style={{ display: "block", fontSize: "0.82rem", color: "var(--muted)", marginBottom: "0.25rem" }}>
                  Base URL Endpoint
                </label>
                <input
                  type="text"
                  style={{ width: "100%", padding: "0.55rem", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--bg)", color: "var(--fg)" }}
                  value={llmBaseUrl}
                  onChange={(e) => setLlmBaseUrl(e.target.value)}
                  placeholder="https://openrouter.ai/api/v1"
                />
              </div>

              <div>
                <label style={{ display: "block", fontSize: "0.82rem", color: "var(--muted)", marginBottom: "0.25rem" }}>
                  API Key ({selectedPresetObj?.env_key_name || "Tuỳ chọn cho Pollinations/Ollama"})
                </label>
                <input
                  type="password"
                  style={{ width: "100%", padding: "0.55rem", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--bg)", color: "var(--fg)" }}
                  value={llmApiKey}
                  onChange={(e) => setLlmApiKey(e.target.value)}
                  placeholder="Để trống nếu giữ nguyên key hiện tại hoặc dùng Keyless..."
                />
              </div>

              <div>
                <label style={{ display: "block", fontSize: "0.82rem", color: "var(--muted)", marginBottom: "0.25rem" }}>
                  Quick Model (Dùng cho /v1/answer & Planner)
                </label>
                <input
                  type="text"
                  style={{ width: "100%", padding: "0.55rem", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--bg)", color: "var(--fg)" }}
                  value={llmQuickModel}
                  onChange={(e) => setLlmQuickModel(e.target.value)}
                  placeholder="openrouter/free"
                />
              </div>

              <div>
                <label style={{ display: "block", fontSize: "0.82rem", color: "var(--muted)", marginBottom: "0.25rem" }}>
                  Report Model (Dùng cho Deep Research /v1/research)
                </label>
                <input
                  type="text"
                  style={{ width: "100%", padding: "0.55rem", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--bg)", color: "var(--fg)" }}
                  value={llmReportModel}
                  onChange={(e) => setLlmReportModel(e.target.value)}
                  placeholder="openrouter/free"
                />
              </div>

              <div>
                <label style={{ display: "block", fontSize: "0.82rem", color: "var(--muted)", marginBottom: "0.25rem" }}>
                  Giao thức Wire Protocol (Dành cho Custom Enterprise LLM)
                </label>
                <select
                  style={{ width: "100%", padding: "0.55rem", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--bg)", color: "var(--fg)" }}
                  value={llmProtocol}
                  onChange={(e) =>
                    setLlmProtocol(e.target.value === "anthropic" ? "anthropic" : "openai")
                  }
                >
                  <option value="openai">OpenAI-Compatible (/chat/completions)</option>
                  <option value="anthropic">Anthropic Messages (/v1/messages)</option>
                </select>
              </div>

              <div>
                <label style={{ display: "block", fontSize: "0.82rem", color: "var(--muted)", marginBottom: "0.25rem" }}>
                  Fallback Provider (Tự động chuyển khi hết hạn mức / chưa có key)
                </label>
                <select
                  style={{ width: "100%", padding: "0.55rem", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--bg)", color: "var(--fg)" }}
                  value={llmFallbackProvider}
                  onChange={(e) => setLlmFallbackProvider(e.target.value)}
                >
                  <option value="pollinations">pollinations (Keyless Free - Khuyến nghị)</option>
                  <option value="openrouter">openrouter (openrouter/free)</option>
                  <option value="ollama">ollama (Local 127.0.0.1:11434)</option>
                  <option value="">Tắt Fallback</option>
                </select>
              </div>
            </div>

            {selectedPresetObj && selectedPresetObj.recommended_models.length > 0 && (
              <div style={{ marginTop: "0.8rem" }}>
                <span className="engine-filter-label" style={{ marginRight: "0.5rem" }}>
                  Model gợi ý (Bấm để chọn):
                </span>
                <div className="engine-chips" style={{ marginTop: "0.35rem" }}>
                  {selectedPresetObj.recommended_models.map((m) => (
                    <button
                      key={m}
                      type="button"
                      className={`chip chip-toggle ${llmQuickModel === m ? "on" : ""}`}
                      onClick={() => {
                        setLlmQuickModel(m);
                        setLlmReportModel(m);
                      }}
                    >
                      {m}
                    </button>
                  ))}
                </div>
              </div>
            )}

            <div style={{ marginTop: "0.8rem" }}>
              <label style={{ display: "block", fontSize: "0.82rem", color: "var(--muted)", marginBottom: "0.25rem" }}>
                Custom Headers JSON (Tuỳ chọn khi tích hợp vào LLM Gateway nội bộ của doanh nghiệp)
              </label>
              <input
                type="text"
                style={{ width: "100%", padding: "0.55rem", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--bg)", color: "var(--fg)" }}
                value={llmHeadersJson}
                onChange={(e) => setLlmHeadersJson(e.target.value)}
                placeholder='{"X-Internal-Auth": "token", "X-Department": "quant-research"}'
              />
            </div>

            <div style={{ display: "flex", gap: "0.75rem", marginTop: "1rem", flexWrap: "wrap" }}>
              <button type="button" className="scan-btn" onClick={handleSaveLLMConfig} disabled={llmSaving}>
                {llmSaving ? "Đang lưu..." : "💾 Lưu & Áp Dụng Ngay (Hot-Config)"}
              </button>
              <button type="button" className="chip chip-toggle on" onClick={handleTestLLM} disabled={llmTesting}>
                {llmTesting ? "Đang kiểm tra..." : "⚡ Test Kết Nối LLM"}
              </button>
              <button type="button" className="chip chip-toggle" onClick={handleResetLLMConfig} disabled={llmSaving}>
                🔄 Khôi Phục Mặc Định (.env)
              </button>
            </div>

            {llmStatusMsg && (
              <p style={{ marginTop: "0.8rem", fontSize: "0.9rem" }}>{llmStatusMsg}</p>
            )}

            {llmTestResult && (
              <div
                style={{
                  marginTop: "0.8rem",
                  padding: "0.8rem",
                  borderRadius: "6px",
                  border: "1px solid var(--border)",
                  fontSize: "0.88rem",
                }}
              >
                {llmTestResult.ok ? (
                  <div>
                    ✅ <strong>Kết nối thành công!</strong> ({llmTestResult.provider} ·{" "}
                    <code>{llmTestResult.model}</code> · {llmTestResult.latency_ms} ms) — Phản hồi:{" "}
                    <code>{llmTestResult.reply}</code>
                  </div>
                ) : (
                  <div style={{ color: "#f87171" }}>
                    ❌ <strong>Kết nối thất bại:</strong> {llmTestResult.error}
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="card">
            <h2 style={{ margin: "0 0 0.6rem", fontSize: "1.15rem" }}>
              2. Hội Đồng Phán Quyết Đa Endpoint (LLM Council + Anonymous Cross-Review + Dream-RSI)
            </h2>
            <p style={{ margin: "0 0 0.8rem", color: "var(--muted)", fontSize: "0.88rem" }}>
              Cho phép sử dụng đồng thời nhiều endpoint miễn phí trong 1 truy vấn: các model sinh câu
              trả lời song song, ẩn danh thành <code>Candidate A, B, C...</code> để chấm điểm chéo
              theo bộ tiêu chuẩn chuyên ngành (Cross-Review), tự động điều chỉnh chính sách trọng số
              qua thế giới mô phỏng <strong>Dream-RSI Replay World</strong>, và tổng hợp phán quyết
              cuối cùng bởi Chủ tịch Hội đồng (Chairman).
            </p>
            <div style={{ display: "grid", gap: "0.6rem" }}>
              <label style={{ fontSize: "0.82rem", color: "var(--muted)" }}>
                Danh sách Endpoints Hội đồng (định dạng <code>provider:model</code>, cách nhau dấu phẩy):
              </label>
              <input
                type="text"
                style={{
                  width: "100%",
                  padding: "0.55rem",
                  borderRadius: "6px",
                  border: "1px solid var(--border)",
                  background: "var(--bg)",
                  color: "var(--fg)",
                }}
                value={councilEndpointsText}
                onChange={(e) => setCouncilEndpointsText(e.target.value)}
                placeholder="openrouter:openrouter/free,pollinations:openai,groq:llama-3.3-70b-versatile,gemini:gemini-2.0-flash"
              />
              <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap", alignItems: "center" }}>
                <button
                  type="button"
                  className="scan-btn"
                  onClick={handleSaveCouncilEndpoints}
                >
                  ⚖️ Cập Nhật Danh Sách Hội Đồng
                </button>
                <button
                  type="button"
                  className={`chip chip-toggle ${councilMode ? "on" : ""}`}
                  onClick={() => setCouncilMode((prev) => !prev)}
                >
                  Chế độ Hội đồng khi Tìm kiếm: {councilMode ? "ĐANG BẬT" : "ĐANG TẮT"}
                </button>
              </div>
            </div>
          </div>

          <div className="card">
            <h2 style={{ margin: "0 0 0.6rem", fontSize: "1.15rem" }}>
              3. Tích Hợp MetaSearch Với Hệ Thống Khác Đã Có Sẵn LLM
            </h2>
            <p style={{ margin: "0 0 0.8rem", color: "var(--muted)", fontSize: "0.88rem" }}>
              Nếu hệ thống của bạn đã có sẵn LLM (Internal LLM Cluster, LangChain/LlamaIndex Agent,
              Dify, n8n, hoặc Chatbot doanh nghiệp), bạn có thể tích hợp theo 2 chiều:
            </p>
            <div style={{ display: "grid", gap: "0.8rem", fontSize: "0.85rem" }}>
              <div style={{ padding: "0.8rem", border: "1px solid var(--border)", borderRadius: "6px" }}>
                <strong>Chiều A — Context-Only RAG & Citation Validator (Hệ thống ngoài tự gọi LLM của họ):</strong>
                <pre style={{ overflowX: "auto", margin: "0.5rem 0 0" }}>
{`# Bước 1: Lấy ngữ cảnh đã Search 19 engines + Pháp y BCTC + Rerank + Đánh số [1]..[n]
POST ${API_BASE_URL}/v1/context
{"query": "pháp y BCTC VCB và kỹ thuật FPT", "k": 8, "budget": 6000,
 "include": ["chunks", "sources", "messages"]}
# -> Mặc định chỉ trả { chunks, sources } kèm source_url, fetched_at, data_kind;
#    thêm "context_md", "messages", "system_prompt" vào include nếu cần

# Bước 2: Đưa 'messages' vào LLM sẵn có của hệ thống bạn để sinh answer_md, sau đó hậu kiểm trích dẫn:
POST ${API_BASE_URL}/v1/citations/validate
{"answer_md": "Kết quả từ LLM nội bộ của bạn [1][2]...", "chunks": [...]}`}
                </pre>
              </div>
              <div style={{ padding: "0.8rem", border: "1px solid var(--border)", borderRadius: "6px" }}>
                <strong>Chiều B — Truyền BYO-LLM Endpoint theo từng Request:</strong>
                <pre style={{ overflowX: "auto", margin: "0.5rem 0 0" }}>
{`POST ${API_BASE_URL}/v1/answer
{
  "query": "Phân tích cổ phiếu FPT",
  "llm": {
    "provider": "openrouter",
    "model": "openrouter/free",
    "base_url": "https://openrouter.ai/api/v1"
  }
}`}
                </pre>
              </div>
            </div>
          </div>
        </section>
      )}

      {/* --- TAB 3: SUPER-AGENT (DEEPSEEK PROACTIVE ROUTER & WORKERS) --- */}
      {activeTab === "superagent" && (
        <section style={{ display: "grid", gap: "1.2rem" }}>
          <div className="card">
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                flexWrap: "wrap",
                gap: "0.5rem",
              }}
            >
              <div>
                <h2 style={{ margin: "0 0 0.4rem", fontSize: "1.25rem" }}>
                  ⚡ MetaSearch Autonomous Super-Agent Engine
                </h2>
                <p style={{ margin: 0, color: "var(--muted)", fontSize: "0.88rem" }}>
                  Kiến trúc Router-Worker phân tán, Triết lý <strong>Quiet Autonomy</strong> (im
                  lặng làm việc, chỉ báo cáo khi có kết quả hoàn chỉnh hoặc cần phê duyệt), Chế
                  độ <strong>Observe/Shadow Mode</strong> và <strong>Watchdog Circuit Breaker</strong>{" "}
                  giám sát ổn định.
                </p>
              </div>
              <button
                type="button"
                className="chip chip-toggle on"
                onClick={() => loadSuperAgentData()}
              >
                🔄 Làm mới Tickets
              </button>
            </div>

            {/* Watchdog Status Summary */}
            {watchdogStatus && (
              <div
                style={{
                  marginTop: "1rem",
                  padding: "0.8rem",
                  background: "var(--bg-subtle, #f8fafc)",
                  borderRadius: "6px",
                  display: "flex",
                  gap: "1.5rem",
                  fontSize: "0.85rem",
                  flexWrap: "wrap",
                }}
              >
                <div>
                  <strong>Workers đang chạy:</strong> {watchdogStatus.monitored_workers_count}
                </div>
                <div>
                  <strong>Max Latency:</strong> {watchdogStatus.max_duration_sec}s
                </div>
                <div>
                  <strong>Max Token Burn:</strong> {watchdogStatus.max_token_budget.toLocaleString()}
                </div>
                <div>
                  <strong>Circuits đã ngắt:</strong>{" "}
                  <span
                    style={{
                      color: watchdogStatus.tripped_circuits.length > 0 ? "red" : "green",
                    }}
                  >
                    {watchdogStatus.tripped_circuits.length}
                  </span>
                </div>
              </div>
            )}
          </div>

          {/* Create Goal Form */}
          <div className="card">
            <h3 style={{ margin: "0 0 0.8rem", fontSize: "1.05rem" }}>
              🎯 Giao Mục Tiêu Mới (Submit Autonomous Goal)
            </h3>
            <form onSubmit={handleCreateAgentTicket} style={{ display: "grid", gap: "0.8rem" }}>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "1fr 200px 160px",
                  gap: "0.8rem",
                }}
              >
                <input
                  type="text"
                  placeholder="Tiêu đề mục tiêu (VD: Phân tích toàn diện BCTC HPG 2024)"
                  value={agentGoalTitle}
                  onChange={(e) => setAgentGoalTitle(e.target.value)}
                  required
                />
                <select
                  value={agentGoalDomain}
                  onChange={(e) => setAgentGoalDomain(e.target.value)}
                >
                  <option value="auto">Auto Domain</option>
                  <option value="financial">Tài chính & BCTC</option>
                  <option value="technology_ai">Công nghệ & AI</option>
                  <option value="biomedical_health">Y sinh & Dược học</option>
                  <option value="general_discovery">Tổng hợp đa chiều</option>
                </select>
                <select
                  value={agentExecutionMode}
                  onChange={(e) =>
                    setAgentExecutionMode(e.target.value as "live" | "observe")
                  }
                >
                  <option value="live">🔴 Live Execution</option>
                  <option value="observe">🟡 Observe (Shadow)</option>
                </select>
              </div>
              <textarea
                rows={3}
                placeholder="Mô tả mục tiêu cụ thể, yêu cầu bóc tách và tiêu chuẩn thẩm định..."
                value={agentGoalObjective}
                onChange={(e) => setAgentGoalObjective(e.target.value)}
                required
                style={{
                  width: "100%",
                  padding: "0.6rem",
                  borderRadius: "6px",
                  border: "1px solid var(--border)",
                }}
              />
              <div style={{ display: "flex", justifyContent: "flex-end" }}>
                <button type="submit" disabled={agentLoading || !agentGoalTitle.trim()}>
                  {agentLoading ? "Đang điều phối Router..." : "⚡ Submit to SuperAgent Router"}
                </button>
              </div>
            </form>
          </div>

          {/* Tickets List */}
          <div className="card">
            <h3 style={{ margin: "0 0 0.8rem", fontSize: "1.05rem" }}>
              📋 Danh Sách Super-Agent Tickets ({superagentTickets.length})
            </h3>
            {superagentTickets.length === 0 ? (
              <p style={{ color: "var(--muted)", fontSize: "0.9rem" }}>
                Chưa có ticket nào. Hãy tạo mục tiêu mới ở trên hoặc kích hoạt từ webhook sự kiện.
              </p>
            ) : (
              <div style={{ display: "grid", gap: "0.8rem" }}>
                {superagentTickets.map((t) => (
                  <div
                    key={t.ticket_id}
                    style={{
                      padding: "0.8rem",
                      border: "1px solid var(--border)",
                      borderRadius: "6px",
                      background:
                        selectedTicket?.ticket_id === t.ticket_id
                          ? "var(--bg-subtle, #f8fafc)"
                          : "transparent",
                    }}
                  >
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "flex-start",
                        flexWrap: "wrap",
                        gap: "0.5rem",
                      }}
                    >
                      <div>
                        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
                          <span
                            className="chip"
                            style={{
                              fontSize: "0.75rem",
                              fontWeight: 600,
                              background:
                                t.status === "completed"
                                  ? "#dcfce7"
                                  : t.status === "observed"
                                  ? "#fef9c3"
                                  : t.status === "waiting_human"
                                  ? "#fed7aa"
                                  : "#e2e8f0",
                              color:
                                t.status === "completed"
                                  ? "#166534"
                                  : t.status === "observed"
                                  ? "#854d0e"
                                  : t.status === "waiting_human"
                                  ? "#9a3412"
                                  : "#334155",
                            }}
                          >
                            {t.status.toUpperCase()}
                          </span>
                          <span style={{ fontSize: "0.8rem", color: "var(--muted)" }}>
                            {t.execution_mode === "observe" ? "🟡 Shadow" : "🔴 Live"} |{" "}
                            {t.domain} | Prio {t.priority_name}
                          </span>
                        </div>
                        <h4 style={{ margin: "0.4rem 0 0.2rem", fontSize: "1rem" }}>{t.title}</h4>
                        <p style={{ margin: 0, fontSize: "0.85rem", color: "var(--muted)" }}>
                          {t.objective}
                        </p>
                      </div>

                      <button
                        type="button"
                        className="chip chip-toggle"
                        onClick={() =>
                          setSelectedTicket(
                            selectedTicket?.ticket_id === t.ticket_id ? null : t
                          )
                        }
                      >
                        {selectedTicket?.ticket_id === t.ticket_id
                          ? "Đóng chi tiết"
                          : "Xem kết quả & vết"}
                      </button>
                    </div>

                    {/* Quiet Autonomy Human Escalation if waiting */}
                    {t.status === "waiting_human" && t.escalations.length > 0 && (
                      <div
                        style={{
                          marginTop: "0.8rem",
                          padding: "0.8rem",
                          background: "#fff7ed",
                          border: "1px solid #fdba74",
                          borderRadius: "6px",
                        }}
                      >
                        <strong style={{ color: "#c2410c", fontSize: "0.88rem" }}>
                          ⚠️ Yêu cầu phê duyệt từ con người (Quiet Autonomy Escalation):
                        </strong>
                        <p style={{ margin: "0.3rem 0 0.6rem", fontSize: "0.85rem" }}>
                          {t.escalations[0].reason}
                        </p>
                        <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
                          {t.escalations[0].proposed_options.map((opt, oIdx) => (
                            <button
                              key={oIdx}
                              type="button"
                              onClick={() =>
                                handleResolveEscalation(
                                  t.ticket_id,
                                  t.escalations[0].escalation_id,
                                  opt
                                )
                              }
                              style={{
                                padding: "0.3rem 0.7rem",
                                fontSize: "0.8rem",
                                borderRadius: "4px",
                                background: oIdx === 0 ? "#ea580c" : "#e2e8f0",
                                color: oIdx === 0 ? "white" : "black",
                                border: "none",
                                cursor: "pointer",
                              }}
                            >
                              {opt}
                            </button>
                          ))}
                        </div>
                      </div>
                    )}

                    {/* Ticket Details & Deliverables */}
                    {selectedTicket?.ticket_id === t.ticket_id && (
                      <div
                        style={{
                          marginTop: "1rem",
                          borderTop: "1px dashed var(--border)",
                          paddingTop: "0.8rem",
                        }}
                      >
                        <div
                          style={{
                            fontSize: "0.82rem",
                            color: "var(--muted)",
                            marginBottom: "0.8rem",
                          }}
                        >
                          Worker: <code>{t.assigned_worker_id || "Chưa gán"}</code> | Steps:{" "}
                          {t.current_step}/{t.max_steps} | Tokens:{" "}
                          {t.consumed_tokens.toLocaleString()}
                        </div>

                        {t.artifacts.length > 0 ? (
                          <div>
                            <h5 style={{ margin: "0 0 0.4rem", fontSize: "0.95rem" }}>
                              📦 Thành Phẩm Đầu Ra ({t.artifacts[0].artifact_type}):
                            </h5>
                            <div
                              style={{
                                background: "var(--bg)",
                                padding: "0.8rem",
                                borderRadius: "6px",
                                border: "1px solid var(--border)",
                              }}
                            >
                              <AnswerMarkdown
                                text={t.artifacts[0].content_md}
                                citationByUrl={
                                  new Map(
                                    (t.artifacts[0].citations || []).map((c) => [c.n, c])
                                  )
                                }
                              />
                            </div>
                          </div>
                        ) : (
                          <p style={{ fontSize: "0.85rem", color: "var(--muted)" }}>
                            Đang xử lý hoặc chưa phát sinh thành phẩm.
                          </p>
                        )}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </section>
      )}

      {/* Reader Modal */}
      {selectedPost && (
        <div className="modal-overlay" onClick={() => setSelectedPost(null)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <button className="modal-close" onClick={() => setSelectedPost(null)}>
              ✕
            </button>
            <div style={{ marginBottom: "1.5rem" }}>
              <span className="badge badge-ai" style={{ marginRight: "0.5rem" }}>
                {selectedPost.topic}
              </span>
              <span style={{ color: "var(--muted)", fontSize: "0.85rem" }}>
                {selectedPost.created_at
                  ? new Date(selectedPost.created_at).toLocaleString("vi-VN")
                  : ""}
              </span>
            </div>

            <AnswerMarkdown
              text={selectedPost.content_md}
              citationByUrl={
                new Map(
                  (selectedPost.sources ?? []).map((s) => [
                    s.n,
                    { n: s.n, url: s.url, title: s.title },
                  ])
                )
              }
            />

            {selectedPost.sources && selectedPost.sources.length > 0 && (
              <div style={{ marginTop: "2rem", borderTop: "1px solid var(--border)", paddingTop: "1rem" }}>
                <h3 style={{ fontSize: "1rem", marginBottom: "0.8rem" }}>
                  Nguồn dữ liệu tham khảo ({selectedPost.sources.length})
                </h3>
                <ol style={{ paddingLeft: "1.2rem", color: "var(--muted)", fontSize: "0.85rem" }}>
                  {selectedPost.sources.map((s, idx) => (
                    <li key={idx} style={{ marginBottom: "0.4rem" }}>
                      <a href={safeHref(s.url)} target="_blank" rel="noreferrer" style={{ color: "var(--accent)" }}>
                        {s.title || s.url}
                      </a>
                    </li>
                  ))}
                </ol>
              </div>
            )}
          </div>
        </div>
      )}

      {modalLoading && (
        <div className="modal-overlay">
          <div className="card status">
            <div className="spinner" />
            <span>Đang tải nội dung bài viết...</span>
          </div>
        </div>
      )}

      <footer className="footer">
        API: <code>{API_BASE_URL}/docs</code> · OpenRouter Free (<code>openrouter/free</code>) & BYO-LLM Ready
      </footer>
    </main>
  );
}
