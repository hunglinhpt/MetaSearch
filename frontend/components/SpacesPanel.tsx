"use client";

/**
 * Spaces, Knowledge Graph & Hybrid Chunk Search Panel (docs/22, docs/24):
 * - /v1/spaces CRUD + /v1/spaces/{slug}/crawl
 * - /v1/spaces/{slug}/graph/build (202 async) + bounded /graph/status poll
 * - /v1/spaces/{slug}/graph/query (1-hop subgraph + guarded LLM answer)
 * - /v1/hybrid (dense cosine + sparse BM25 RRF chunk search)
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import AnswerMarkdown from "./AnswerMarkdown";
import {
  buildSpaceGraph,
  crawlSpace,
  createSpace,
  deleteSpace,
  hybridSearch,
  listSpaces,
  querySpaceGraph,
  safeHref,
  spaceGraphStatus,
  type Citation,
  type GraphBuildStatus,
  type GraphQueryResponse,
  type HybridSearchResponse,
  type SpaceCrawlResponse,
  type SpaceItem,
} from "../lib/api";

export default function SpacesPanel() {
  const [spaces, setSpaces] = useState<SpaceItem[]>([]);
  const [storage, setStorage] = useState("db");
  const [selectedSlug, setSelectedSlug] = useState("");
  const [loadingList, setLoadingList] = useState(false);

  // Create form
  const [name, setName] = useState("");
  const [domainsRaw, setDomainsRaw] = useState("");
  const [pinnedRaw, setPinnedRaw] = useState("");
  const [restrict, setRestrict] = useState(true);
  const [creating, setCreating] = useState(false);

  // Operations on selected space
  const [crawling, setCrawling] = useState(false);
  const [crawlRes, setCrawlRes] = useState<SpaceCrawlResponse | null>(null);
  const [graphStatus, setGraphStatus] = useState<GraphBuildStatus | null>(null);
  const [buildingGraph, setBuildingGraph] = useState(false);

  // KG query & Hybrid search
  const [kgQuery, setKgQuery] = useState("");
  const [kgAnswerToggle, setKgAnswerToggle] = useState(true);
  const [kgLoading, setKgLoading] = useState(false);
  const [kgRes, setKgRes] = useState<GraphQueryResponse | null>(null);

  const [hybridQuery, setHybridQuery] = useState("");
  const [hybridLoading, setHybridLoading] = useState(false);
  const [hybridRes, setHybridRes] = useState<HybridSearchResponse | null>(null);

  const [error, setError] = useState("");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollAbortRef = useRef<AbortController | null>(null);
  const opAbortRef = useRef<AbortController | null>(null);
  const activeSlugRef = useRef<string>(selectedSlug);
  activeSlugRef.current = selectedSlug;

  const stopPoll = useCallback(() => {
    if (pollRef.current !== null) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    pollAbortRef.current?.abort();
    pollAbortRef.current = null;
  }, []);

  const refresh = useCallback(async () => {
    setLoadingList(true);
    try {
      const resp = await listSpaces();
      setSpaces(resp.items);
      setStorage(resp.storage);
      setSelectedSlug((cur) => {
        if (cur && resp.items.some((s) => s.slug === cur)) return cur;
        return resp.items[0]?.slug ?? "";
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoadingList(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    return () => {
      stopPoll();
      opAbortRef.current?.abort();
    };
  }, [refresh, stopPoll]);

  useEffect(() => {
    stopPoll();
    opAbortRef.current?.abort();
    setCrawling(false);
    setBuildingGraph(false);
    setKgLoading(false);
    setHybridLoading(false);
    setCrawlRes(null);
    setKgRes(null);
    setHybridRes(null);
    setError("");
    if (!selectedSlug) {
      setGraphStatus(null);
      return;
    }
    const ctrl = new AbortController();
    const target = selectedSlug;
    spaceGraphStatus(target, ctrl.signal)
      .then((s) => {
        if (!ctrl.signal.aborted && activeSlugRef.current === target) setGraphStatus(s);
      })
      .catch(() => {
        if (!ctrl.signal.aborted && activeSlugRef.current === target) setGraphStatus(null);
      });
    return () => {
      ctrl.abort();
    };
  }, [selectedSlug, stopPoll]);

  const handleCreate = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (!name.trim() || creating) return;
      setCreating(true);
      setError("");
      try {
        const domains = domainsRaw
          .split(/[\s,]+/)
          .map((x) => x.trim())
          .filter(Boolean);
        const pinned = pinnedRaw
          .split(/[\s,]+/)
          .map((x) => x.trim())
          .filter(Boolean);
        const created = await createSpace({
          name: name.trim(),
          domains,
          pinned_urls: pinned,
          restrict,
        });
        setName("");
        setDomainsRaw("");
        setPinnedRaw("");
        await refresh();
        setSelectedSlug(created.slug);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setCreating(false);
      }
    },
    [name, domainsRaw, pinnedRaw, restrict, creating, refresh]
  );

  const handleDelete = useCallback(
    async (slug: string) => {
      if (slug === selectedSlug) {
        stopPoll();
        opAbortRef.current?.abort();
      }
      setError("");
      try {
        await deleteSpace(slug);
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    },
    [refresh, selectedSlug, stopPoll]
  );

  const handleCrawl = useCallback(async () => {
    if (!selectedSlug || crawling) return;
    const targetSlug = selectedSlug;
    opAbortRef.current?.abort();
    const ctrl = new AbortController();
    opAbortRef.current = ctrl;
    setCrawling(true);
    setError("");
    try {
      const resp = await crawlSpace(targetSlug, 20, ctrl.signal);
      if (ctrl.signal.aborted || activeSlugRef.current !== targetSlug) return;
      setCrawlRes(resp);
      await refresh();
    } catch (err) {
      if (ctrl.signal.aborted || activeSlugRef.current !== targetSlug) return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (!ctrl.signal.aborted && activeSlugRef.current === targetSlug) setCrawling(false);
    }
  }, [selectedSlug, crawling, refresh]);

  const handleBuildGraph = useCallback(async () => {
    if (!selectedSlug || buildingGraph) return;
    stopPoll();
    const targetSlug = selectedSlug;
    const ctrl = new AbortController();
    pollAbortRef.current = ctrl;
    setBuildingGraph(true);
    setError("");
    try {
      const initial = await buildSpaceGraph(targetSlug, ctrl.signal);
      if (ctrl.signal.aborted || activeSlugRef.current !== targetSlug) return;
      setGraphStatus(initial);
      if (initial.status === "ready" || initial.status === "error") {
        setBuildingGraph(false);
        return;
      }
      let attempts = 0;
      let errs = 0;
      pollRef.current = setInterval(async () => {
        if (ctrl.signal.aborted || activeSlugRef.current !== targetSlug) {
          stopPoll();
          return;
        }
        attempts += 1;
        if (attempts > 150) {
          stopPoll();
          setBuildingGraph(false);
          setError("Hết thời gian chờ build Knowledge Graph.");
          return;
        }
        try {
          errs = 0;
          const s = await spaceGraphStatus(targetSlug, ctrl.signal);
          if (ctrl.signal.aborted || activeSlugRef.current !== targetSlug) return;
          setGraphStatus(s);
          if (s.status !== "running" && s.status !== "pending") {
            stopPoll();
            setBuildingGraph(false);
          }
        } catch {
          if (ctrl.signal.aborted || activeSlugRef.current !== targetSlug) return;
          errs += 1;
          if (errs >= 5) {
            stopPoll();
            setBuildingGraph(false);
            setError("Mất kết nối khi theo dõi trạng thái Knowledge Graph.");
          }
        }
      }, 2000);
    } catch (err) {
      if (ctrl.signal.aborted || activeSlugRef.current !== targetSlug) return;
      setError(err instanceof Error ? err.message : String(err));
      setBuildingGraph(false);
    }
  }, [selectedSlug, buildingGraph, stopPoll]);

  const handleQueryGraph = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (!selectedSlug || !kgQuery.trim() || kgLoading) return;
      const targetSlug = selectedSlug;
      opAbortRef.current?.abort();
      const ctrl = new AbortController();
      opAbortRef.current = ctrl;
      setKgLoading(true);
      setError("");
      try {
        const resp = await querySpaceGraph(
          targetSlug,
          { query: kgQuery.trim(), answer: kgAnswerToggle },
          ctrl.signal
        );
        if (ctrl.signal.aborted || activeSlugRef.current !== targetSlug) return;
        setKgRes(resp);
      } catch (err) {
        if (ctrl.signal.aborted || activeSlugRef.current !== targetSlug) return;
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!ctrl.signal.aborted && activeSlugRef.current === targetSlug) setKgLoading(false);
      }
    },
    [selectedSlug, kgQuery, kgAnswerToggle, kgLoading]
  );

  const handleHybridSearch = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (!hybridQuery.trim() || hybridLoading) return;
      const targetSlug = selectedSlug;
      opAbortRef.current?.abort();
      const ctrl = new AbortController();
      opAbortRef.current = ctrl;
      setHybridLoading(true);
      setError("");
      try {
        const resp = await hybridSearch(
          {
            query: hybridQuery.trim(),
            space: targetSlug || undefined,
            k: 10,
            dense: true,
          },
          ctrl.signal
        );
        if (ctrl.signal.aborted || activeSlugRef.current !== targetSlug) return;
        setHybridRes(resp);
      } catch (err) {
        if (ctrl.signal.aborted || activeSlugRef.current !== targetSlug) return;
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!ctrl.signal.aborted && activeSlugRef.current === targetSlug) setHybridLoading(false);
      }
    },
    [hybridQuery, selectedSlug, hybridLoading]
  );

  const emptyCitations = useMemo(() => new Map<number, Citation>(), []);
  const selectedSpace = spaces.find((s) => s.slug === selectedSlug) ?? null;

  return (
    <section className="card">
      <div className="compare-head">
        <div>
          <h2 className="section-title">🗂️ Không gian Kiến thức (Spaces) · Knowledge Graph · Hybrid Search</h2>
          <p className="muted small">
            Khoanh vùng domain/URL cố định cho từng API key, crawl &amp; dựng Knowledge Graph 1-hop, hoặc
            tìm kiếm ngữ nghĩa lai BM25 + Vector (<code>POST /v1/hybrid</code>).
            {storage !== "db" && " (Chế độ fallback in-memory — DB chưa sẵn sàng)"}
          </p>
        </div>
        <button type="button" className="read-btn" onClick={() => void refresh()} disabled={loadingList}>
          {loadingList ? "Đang tải..." : "↻ Làm mới"}
        </button>
      </div>

      {error && <div className="card error">{error}</div>}

      <form onSubmit={handleCreate} className="space-form">
        <div className="search-row">
          <input
            type="text"
            placeholder="Tên Space (VD: Nghiên cứu FPT)"
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={creating}
          />
          <input
            type="text"
            placeholder="Domains (VD: cafef.vn, vietstock.vn)"
            value={domainsRaw}
            onChange={(e) => setDomainsRaw(e.target.value)}
            disabled={creating}
          />
          <button type="submit" disabled={creating || !name.trim()}>
            {creating ? "Đang tạo..." : "+ Tạo Space"}
          </button>
        </div>
        <div className="options-row" style={{ marginTop: "0.45rem" }}>
          <input
            type="text"
            style={{ flex: 1, minWidth: "240px" }}
            placeholder="Pinned URLs http(s) tuỳ chọn, cách nhau bởi dấu phẩy hoặc khoảng trắng"
            value={pinnedRaw}
            onChange={(e) => setPinnedRaw(e.target.value)}
            disabled={creating}
          />
          <label className="checkbox-inline">
            <input
              type="checkbox"
              checked={restrict}
              onChange={(e) => setRestrict(e.target.checked)}
            />
            Giới hạn chặt trong domains của Space (<code>restrict</code>)
          </label>
        </div>
      </form>

      <div className="spaces-bar">
        {spaces.length === 0 ? (
          <span className="muted small">Chưa có Space nào — tạo mới để crawl và dựng đồ thị tri thức.</span>
        ) : (
          spaces.map((s) => (
            <div key={s.id} className={`space-pill ${selectedSlug === s.slug ? "active" : ""}`}>
              <button
                type="button"
                className="space-select-btn"
                onClick={() => setSelectedSlug(s.slug)}
              >
                {s.name} (<code>{s.slug}</code>)
              </button>
              <button
                type="button"
                className="space-del-btn"
                onClick={() => void handleDelete(s.slug)}
                aria-label={`Xoá space ${s.name}`}
                title="Xoá Space"
              >
                ✕
              </button>
            </div>
          ))
        )}
      </div>

      {selectedSpace && (
        <div className="space-detail">
          <div className="space-meta-row">
            <span className="muted small">
              Domains: {selectedSpace.domains.join(", ") || "(không)"}{" "}
              · Pinned: {selectedSpace.pinned_urls.length}{" "}
              · Last crawl: {selectedSpace.last_crawl_at ? selectedSpace.last_crawl_at.slice(0, 19) : "chưa"}
            </span>
            <div className="space-actions">
              <button
                type="button"
                className="tts-btn"
                onClick={() => void handleCrawl()}
                disabled={crawling}
              >
                {crawling ? "Đang crawl..." : "🕷️ Crawl Space"}
              </button>
              <button
                type="button"
                className="tts-btn"
                onClick={() => void handleBuildGraph()}
                disabled={buildingGraph}
              >
                {buildingGraph ? "Đang build KG..." : "🧠 Build Knowledge Graph"}
              </button>
            </div>
          </div>

          {graphStatus && (
            <p className="muted small">
              KG Status: <strong>{graphStatus.status}</strong> · {graphStatus.nodes} thực thể ·{" "}
              {graphStatus.edges} quan hệ · {graphStatus.docs} tài liệu
              {graphStatus.error ? ` · lỗi: ${graphStatus.error}` : ""}
            </p>
          )}

          {crawlRes && (
            <p className="muted small">
              Đã crawl {crawlRes.pages.length}/{crawlRes.targets} URL mục tiêu (giới hạn {crawlRes.capped}).
            </p>
          )}

          <form onSubmit={handleQueryGraph} style={{ marginTop: "0.8rem" }}>
            <div className="search-row">
              <input
                type="text"
                placeholder={`Hỏi Knowledge Graph của "${selectedSpace.name}"...`}
                value={kgQuery}
                onChange={(e) => setKgQuery(e.target.value)}
                disabled={kgLoading}
              />
              <label className="checkbox-inline">
                <input
                  type="checkbox"
                  checked={kgAnswerToggle}
                  onChange={(e) => setKgAnswerToggle(e.target.checked)}
                />
                LLM tóm tắt
              </label>
              <button type="submit" disabled={kgLoading || !kgQuery.trim()}>
                {kgLoading ? "Đang truy vấn..." : "Truy vấn KG"}
              </button>
            </div>
          </form>

          {kgRes && (
            <div className="kg-result-box">
              <p className="muted small">
                Khớp: {kgRes.matched_labels.join(", ") || "(không)"} · {kgRes.nodes.length} nút ·{" "}
                {kgRes.edges.length} cạnh
              </p>
              {kgRes.answer && <AnswerMarkdown text={kgRes.answer} citationByUrl={emptyCitations} />}
              {kgRes.edges.length > 0 && (
                <ul className="kg-edge-list">
                  {kgRes.edges.slice(0, 20).map((e, idx) => (
                    <li key={`${e.source}-${e.relation}-${e.target}-${idx}`}>
                      <strong>{e.source}</strong> <em>—[{e.relation}]→</em>{" "}
                      <strong>{e.target}</strong>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      )}

      <div className="hybrid-box">
        <h3 className="small" style={{ marginBottom: "0.45rem" }}>
          🧬 Hybrid Chunk Search (BM25 + Dense Cosine RRF
          {selectedSpace ? ` · đang lọc trong "${selectedSpace.slug}"` : ""})
        </h3>
        <form onSubmit={handleHybridSearch}>
          <div className="search-row">
            <input
              type="text"
              placeholder="Tìm đoạn nội dung đã index trong kho (VD: biên lợi nhuận gộp, Kafka consumer...)"
              value={hybridQuery}
              onChange={(e) => setHybridQuery(e.target.value)}
              disabled={hybridLoading}
            />
            <button type="submit" disabled={hybridLoading || !hybridQuery.trim()}>
              {hybridLoading ? "Đang tìm..." : "Tìm Hybrid"}
            </button>
          </div>
        </form>
        {hybridRes && (
          <div style={{ marginTop: "0.7rem" }}>
            <p className="muted small">
              {hybridRes.chunks.length} đoạn khớp · Dense vector:{" "}
              {hybridRes.dense_used ? "đã bật" : "sparse-only fallback"}
            </p>
            <ul className="driver-list">
              {hybridRes.chunks.map((c) => {
                const href = safeHref(c.url);
                return (
                  <li key={c.chunk_id} className="driver-item">
                    <div className="driver-head">
                      <span className="badge">RRF {c.score.toFixed(4)}</span>
                      <span className="muted small">
                        {c.domain} · #{c.position} · BM25={c.sparse.toFixed(3)}
                        {c.dense !== null ? ` · cos=${c.dense.toFixed(3)}` : ""}
                      </span>
                    </div>
                    <div className="driver-title">
                      {href ? (
                        <a href={href} target="_blank" rel="noreferrer">
                          {c.title || c.url}
                        </a>
                      ) : (
                        c.title || c.url
                      )}
                    </div>
                    <p className="muted small" style={{ margin: "0.25rem 0 0" }}>
                      {c.content.slice(0, 320)}
                      {c.content.length > 320 ? "…" : ""}
                    </p>
                  </li>
                );
              })}
            </ul>
          </div>
        )}
      </div>
    </section>
  );
}
