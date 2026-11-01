"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  API_BASE_URL,
  AnswerMeta,
  BlogPostDetail,
  BlogPostSummary,
  Citation,
  EngineDone,
  EngineInfo,
  SearchHit,
  SourceItem,
  getDigest,
  listDigests,
  listEngines,
  search,
  streamAnswer,
  triggerScan,
} from "../lib/api";

type Mode = "answer" | "raw";
type AppTab = "search" | "blog";

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

  useEffect(() => {
    const controller = new AbortController();
    listEngines(controller.signal)
      .then((engines) =>
        setEngineOptions(engines.filter((e) => e.enabled && e.adapter_ready))
      )
      .catch(() => setEngineOptions([]));
    return () => controller.abort();
  }, []);

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
    try {
      if (mode === "raw") {
        const resp = await search(q, {
          signal: controller.signal,
          engines: selectedEngines.length ? selectedEngines : undefined,
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
          selectedEngines.length ? selectedEngines : undefined
        );
      }
    } catch (err: unknown) {
      if (err instanceof Error && err.name === "AbortError") return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [busy, mode, query, reset, selectedEngines]);

  const citationByUrl = new Map(citations.map((c) => [c.n, c]));

  return (
    <main className="shell">
      <header>
        <h1>MetaSearch</h1>
        <p className="tagline">
          Hệ thống tìm kiếm tổng hợp đa tầng & Deep Research thông minh
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
                  placeholder="Nhập câu hỏi (ví dụ: $NVDA, cổ phiếu FPT, hướng nhà 1990, AI agent...)"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  disabled={busy}
                />
                <button type="submit" disabled={busy || !query.trim()}>
                  {busy ? "Đang xử lý..." : "Tìm kiếm"}
                </button>
              </div>

              <div className="mode-toggle">
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

              {engineOptions.length > 0 && (
                <div className="engine-filter">
                  <span className="engine-filter-label">Engines (17 tổng):</span>
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
                          title={e.name || e.id}
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

            <div
              style={{
                lineHeight: 1.7,
                whiteSpace: "pre-wrap",
                fontSize: "0.95rem",
              }}
            >
              {selectedPost.content_md}
            </div>

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
        API: <code>{API_BASE_URL}/docs</code> · Scan Scheduler & Daily Digest Engine
      </footer>
    </main>
  );
}

/** Render streaming markdown-lite text with [n] citation markers as links. */
function AnswerMarkdown({
  text,
  citationByUrl,
}: {
  text: string;
  citationByUrl: Map<number, Citation>;
}) {
  const parts = text.split(/(\[\d{1,3}\])/g);
  return (
    <div className="answer-body">
      {parts.map((part, i) => {
        const match = part.match(/^\[(\d{1,3})\]$/);
        if (!match) {
          return <span key={i}>{part}</span>;
        }
        const n = Number(match[1]);
        const citation = citationByUrl.get(n);
        return (
          <a
            key={i}
            className="cite"
            href={citation?.url ?? undefined}
            target="_blank"
            rel="noreferrer"
            title={citation ? `${citation.title || citation.url}` : `nguồn [${n}]`}
          >
            [{n}]
          </a>
        );
      })}
    </div>
  );
}
