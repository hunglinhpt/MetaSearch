"use client";

import { ReactNode, useCallback, useEffect, useRef, useState } from "react";

import {
  API_BASE_URL,
  ActiveLLMConfig,
  AnswerMeta,
  BlogPostDetail,
  BlogPostSummary,
  Citation,
  EngineDone,
  EngineInfo,
  LLMOverridePayload,
  LLMProviderPresetInfo,
  LLMTestResult,
  SearchHit,
  SourceItem,
  getDigest,
  getLLMProviders,
  listDigests,
  listEngines,
  resetLLMConfig,
  search,
  streamAnswer,
  testLLMConnection,
  triggerScan,
  updateLLMConfig,
} from "../lib/api";

type Mode = "answer" | "raw";
type AppTab = "search" | "blog" | "llm";

const CATEGORY_CHIPS = [
  { id: "auto", label: "⚡ Tự động (Smart Routing)" },
  { id: "financial", label: "📈 Tài chính & Chứng khoán" },
  { id: "news", label: "📰 Tin tức & Thời sự" },
  { id: "developer", label: "💻 Lập trình & AI" },
  { id: "academic", label: "🔬 Học thuật & Y sinh" },
  { id: "fengshui", label: "🧭 Phong thủy & Bát trạch" },
  { id: "reference", label: "📚 Bách khoa" },
  { id: "social", label: "💬 Thảo luận" },
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
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState("");
  const [engines, setEngines] = useState<EngineDone[]>([]);
  const [sources, setSources] = useState<SourceItem[]>([]);
  const [answerMd, setAnswerMd] = useState("");
  const [citations, setCitations] = useState<Citation[]>([]);
  const [meta, setMeta] = useState<AnswerMeta | null>(null);
  const [error, setError] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [searchInfo, setSearchInfo] = useState("");
  const [engineOptions, setEngineOptions] = useState<EngineInfo[]>([]);
  const [selectedEngines, setSelectedEngines] = useState<string[]>([]);
  const abortRef = useRef<AbortController | null>(null);

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
  }, [activeTab, blogTopic, loadBlogPosts]);

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
    setMeta(null);
    setError("");
    setHits([]);
    setSearchInfo("");
  }, []);

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
    try {
      if (mode === "raw") {
        const resp = await search(q, {
          signal: controller.signal,
          engines: selectedEngines.length ? selectedEngines : undefined,
          categories: categoriesParam,
        });
        setHits(resp.results);
        setSearchInfo(
          `${resp.results.length} kết quả · ${resp.latency_ms} ms · engines: ` +
            `${resp.engines_used.join(", ") || "-"} · ${resp.cached ? "cached" : "fresh"}`
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
            onDone: (m) => setMeta(m),
            onError: (code, message) => setError(`[${code}] ${message}`),
          },
          controller.signal,
          selectedEngines.length ? selectedEngines : undefined,
          categoriesParam
        );
      }
    } catch (err: unknown) {
      if (err instanceof Error && err.name === "AbortError") return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [busy, mode, query, reset, selectedCategory, selectedEngines]);

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
            className={`nav-tab ${activeTab === "blog" ? "active" : ""}`}
            onClick={() => setActiveTab("blog")}
          >
            📰 Bản Tin & Blog Chuyên Sâu
          </button>
          <button
            className={`nav-tab ${activeTab === "llm" ? "active" : ""}`}
            onClick={() => setActiveTab("llm")}
          >
            🤖 Cấu Hình LLM (OpenRouter Free / BYO-LLM)
          </button>
        </nav>
      </header>

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

                {activeLlm && (
                  <button
                    type="button"
                    className="chip chip-toggle on"
                    onClick={() => setActiveTab("llm")}
                    title="Bấm để đổi LLM Provider (OpenRouter Free, Pollinations, Gemini, Groq, Ollama, Custom...)"
                  >
                    🤖 LLM: {activeLlm.provider} ({activeLlm.quick_model})
                    {activeLlm.is_free ? " · FREE" : ""}
                  </button>
                )}
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
                    {meta && (
                      <span className="answer-meta">
                        {meta.latency_ms} ms · {meta.model}
                        {meta.cached ? " · cached" : ""}
                      </span>
                    )}
                  </header>
                  <AnswerMarkdown text={answerMd} citationByUrl={citationByUrl} />
                  {citations.length > 0 && (
                    <div className="citations">
                      <h3>Trích dẫn nguồn ({citations.length})</h3>
                      <ol>
                        {citations.map((c) => (
                          <li key={c.n}>
                            <a href={c.url} target="_blank" rel="noreferrer">
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
                      <a className="hit-title" href={h.url} target="_blank" rel="noreferrer">
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
                      <a href={s.url} target="_blank" rel="noreferrer" title={s.url}>
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
              1. Chọn Nhà Cung Cấp LLM (OpenRouter Free, Các Nguồn Free Uy Tín & BYO-LLM)
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
              </a>{" "}
              cùng các LLM miễn phí uy tín (Pollinations Keyless, Google Gemini Free, Groq LPU Free,
              Cerebras Free, Mistral Free, GitHub Models, HuggingFace, Ollama Local) hoặc kết nối
              trực tiếp vào LLM có sẵn trong hệ thống của bạn.
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
                      href={selectedPresetObj.signup_url}
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
              2. Tích Hợp MetaSearch Với Hệ Thống Khác Đã Có Sẵn LLM
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
{"query": "pháp y BCTC VCB và kỹ thuật FPT", "k": 8, "budget": 6000}
# -> Trả về: { context_md, system_prompt, messages, chunks, sources }

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
                      <a href={s.url} target="_blank" rel="noreferrer" style={{ color: "var(--accent)" }}>
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

/** Render inline text with **bold** and [n] citation markers as clickable links. */
function renderInlineTokens(
  line: string,
  citationByUrl: Map<number, Citation>
): ReactNode[] {
  const segments = line.split(/(\*\*[^*]+\*\*|\[\d{1,3}\])/g);
  return segments.map((seg, idx) => {
    const citeMatch = seg.match(/^\[(\d{1,3})\]$/);
    if (citeMatch) {
      const n = Number(citeMatch[1]);
      const citation = citationByUrl.get(n);
      return (
        <a
          key={idx}
          className="cite"
          href={citation?.url ?? undefined}
          target="_blank"
          rel="noreferrer"
          title={citation ? `${citation.title || citation.url}` : `nguồn [${n}]`}
        >
          [{n}]
        </a>
      );
    }
    if (seg.startsWith("**") && seg.endsWith("**") && seg.length > 4) {
      return <strong key={idx}>{seg.slice(2, -2)}</strong>;
    }
    return <span key={idx}>{seg}</span>;
  });
}

/** Render streaming markdown with headings, tables, lists, bold, and [n] citations. */
function AnswerMarkdown({
  text,
  citationByUrl,
}: {
  text: string;
  citationByUrl: Map<number, Citation>;
}) {
  const lines = text.split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    // Table block detection
    if (trimmed.startsWith("|") && trimmed.endsWith("|")) {
      const tableLines: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        tableLines.push(lines[i].trim());
        i++;
      }
      if (tableLines.length >= 2) {
        const parseRow = (r: string) =>
          r
            .slice(1, -1)
            .split("|")
            .map((c) => c.trim());
        const headers = parseRow(tableLines[0]);
        const bodyRows = tableLines
          .slice(1)
          .filter((r) => !/^\|[\s\-:|]+\|$/.test(r))
          .map(parseRow);
        blocks.push(
          <div key={`tbl-${i}`} className="md-table-wrap">
            <table className="md-table">
              <thead>
                <tr>
                  {headers.map((h, hIdx) => (
                    <th key={hIdx}>{renderInlineTokens(h, citationByUrl)}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {bodyRows.map((row, rIdx) => (
                  <tr key={rIdx}>
                    {row.map((cell, cIdx) => (
                      <td key={cIdx}>{renderInlineTokens(cell, citationByUrl)}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
        continue;
      }
    }

    if (trimmed.startsWith("#### ")) {
      blocks.push(
        <h4 key={i}>{renderInlineTokens(trimmed.slice(5), citationByUrl)}</h4>
      );
    } else if (trimmed.startsWith("### ")) {
      blocks.push(
        <h3 key={i}>{renderInlineTokens(trimmed.slice(4), citationByUrl)}</h3>
      );
    } else if (trimmed.startsWith("## ")) {
      blocks.push(
        <h2 key={i}>{renderInlineTokens(trimmed.slice(3), citationByUrl)}</h2>
      );
    } else if (trimmed.startsWith("- ") || trimmed.startsWith("* ")) {
      blocks.push(
        <div key={i} style={{ paddingLeft: "0.8rem", margin: "0.2rem 0" }}>
          • {renderInlineTokens(trimmed.slice(2), citationByUrl)}
        </div>
      );
    } else if (trimmed.length > 0) {
      blocks.push(<p key={i}>{renderInlineTokens(line, citationByUrl)}</p>);
    }
    i++;
  }

  return <div className="answer-body">{blocks}</div>;
}
