"use client";

/**
 * Deep Research tab (docs/18 §A2): consumes the FR-9 backend that previously
 * had no UI — POST /v1/research → SSE /events (status + report_token) →
 * final GET for citations/learnings — and reads the finished report aloud
 * SECTION BY SECTION (playlist with next/stop) through the preferred TTS
 * engine (Web Speech default, VoiceStudio when configured — docs/17).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  cancelResearchJob,
  createResearch,
  getResearch,
  listResearchJobs,
  researchEventsUrl,
  safeHref,
  streamSse,
  ttsCapabilities,
  type Citation,
  type ResearchConflict,
  type ResearchHistoryItem,
  type ResearchStatus,
  type ResearchToolSummary,
  type ResearchVerification,
} from "../lib/api";
import {
  getPreferredEngine,
  TtsHandle,
  ttsIsSupported,
  ttsSpeak,
  ttsSpeakRemote,
  ttsStop,
} from "../lib/tts";
import { splitReportSections } from "../lib/sectionTTS";
import AnswerMarkdown from "./AnswerMarkdown";

type Phase = "idle" | "running" | "done" | "error";

const DEPTHS = [1, 2, 3, 4, 5] as const;
const BREADTHS = [2, 3, 4, 5] as const;
const TIME_BUDGETS = [60, 180, 300] as const;
const PROFILES = [
  "auto",
  "financial",
  "comparative",
  "developer",
  "academic",
  "news",
  "general",
] as const;

export default function ResearchPanel() {
  const [query, setQuery] = useState("");
  const [depth, setDepth] = useState<number>(3);
  const [breadth, setBreadth] = useState<number>(4);
  const [maxTimeS, setMaxTimeS] = useState<number>(180);
  const [profile, setProfile] = useState<string>("auto");
  const [space, setSpace] = useState<string>("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [stage, setStage] = useState("");
  const [reportMd, setReportMd] = useState("");
  const [citations, setCitations] = useState<Citation[]>([]);
  // Audit round 3: stable identity so the memoized AnswerMarkdown actually
  // short-circuits between report tokens.
  const citationByUrl = useMemo(
    () => buildCitationMap(citations),
    [citations]
  );
  const [learnings, setLearnings] = useState<string[]>([]);
  const [outline, setOutline] = useState<string[]>([]);
  const [conflicts, setConflicts] = useState<ResearchConflict[]>([]);
  const [verification, setVerification] = useState<ResearchVerification | null>(null);
  const [tools, setTools] = useState<ResearchToolSummary[]>([]);
  const [resolvedProfile, setResolvedProfile] = useState<string | null>(null);
  const [history, setHistory] = useState<ResearchHistoryItem[]>([]);
  const [selectedHistoryId, setSelectedHistoryId] = useState("");
  const [error, setError] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);

  // Section playlist state.
  const [playingSection, setPlayingSection] = useState<number | null>(null);
  const [ttsReady, setTtsReady] = useState(false);
  const [vsEnabled, setVsEnabled] = useState(false);
  const ttsHandleRef = useRef<TtsHandle | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const settledRef = useRef(false);
  const streamDonePromiseRef = useRef<Promise<void> | null>(null);
  const sectionsRef = useRef<{ title: string; text: string }[]>([]);
  const reportRef = useRef("");
  const rafRef = useRef<number | null>(null);

  const refreshHistory = useCallback((signal?: AbortSignal) => {
    listResearchJobs(15, signal)
      .then((res) => setHistory(res.jobs ?? []))
      .catch(() => {
        /* ignore offline history errors */
      });
  }, []);

  useEffect(() => {
    setTtsReady(ttsIsSupported());
    ttsCapabilities()
      .then((c) => setVsEnabled(Boolean(c.voicestudio.enabled)))
      .catch(() => setVsEnabled(false));
    const histCtrl = new AbortController();
    refreshHistory(histCtrl.signal);
    return () => {
      histCtrl.abort();
      abortRef.current?.abort();
      ttsHandleRef.current?.cancel();
      ttsStop();
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
  }, [refreshHistory]);

  const stopSpeaking = useCallback(() => {
    ttsHandleRef.current?.cancel();
    ttsHandleRef.current = null;
    ttsStop();
    setPlayingSection(null);
  }, []);

  const speakSectionAt = useCallback(
    (index: number) => {
      const sections = sectionsRef.current;
      if (!sections.length || index < 0 || index >= sections.length) {
        stopSpeaking();
        return;
      }
      stopSpeaking();
      const section = sections[index];
      const text = `${section.title}. ${section.text}`;
      const cbs = {
        onStart: () => setPlayingSection(index),
        onEnd: () => setPlayingSection(null),
        onError: () => setPlayingSection(null),
        fallbackToWebSpeech: true,
      };
      ttsHandleRef.current =
        getPreferredEngine() === "voicestudio" && vsEnabled
          ? ttsSpeakRemote(text, cbs)
          : ttsSpeak(text, cbs);
    },
    [stopSpeaking, vsEnabled]
  );

  const start = async () => {
    const q = query.trim();
    if (!q || phase === "running") return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setPhase("running");
    setStage("Đang gửi yêu cầu...");
    setReportMd("");
    reportRef.current = "";
    sectionsRef.current = [];
    setCitations([]);
    setLearnings([]);
    setOutline([]);
    setConflicts([]);
    setVerification(null);
    setTools([]);
    setResolvedProfile(null);
    setError("");
    setJobId(null);
    settledRef.current = false;
    stopSpeaking();
    try {
      const created = await createResearch(
        {
          query: q,
          breadth,
          depth,
          max_time_s: maxTimeS,
          profile,
          space: space.trim() || undefined,
        },
        controller.signal
      );
      setJobId(created.research_id);
      setSelectedHistoryId(created.research_id);
      setStage("Đang lên kế hoạch nghiên cứu...");
      refreshHistory();
      // attachStream re-attaches on a dropped connection (the backend replays
      // its event buffer) and only settles on a TERMINAL job status.
      await attachStream(created.research_id, controller.signal, 3);
    } catch (e) {
      if (controller.signal.aborted) return;
      setError(e instanceof Error ? e.message : String(e));
      setPhase("error");
    }
  };

  const handleCancel = async () => {
    if (!jobId) return;
    try {
      await cancelResearchJob(jobId);
      setStage("Đã hủy tác vụ nghiên cứu.");
      setPhase("error");
      setError("Người dùng đã hủy nghiên cứu.");
    } catch {
      /* fallback abort */
    } finally {
      abortRef.current?.abort();
      refreshHistory();
    }
  };

  const loadHistoricalJob = async (id: string) => {
    setSelectedHistoryId(id);
    if (!id) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const st = await getResearch(id, controller.signal);
      if (controller.signal.aborted) return;
      setJobId(st.research_id);
      setReportMd(st.report_md ?? "");
      reportRef.current = st.report_md ?? "";
      setCitations(st.citations ?? []);
      setLearnings(st.learnings ?? []);
      setOutline(st.outline ?? []);
      setConflicts(st.conflicts ?? []);
      setVerification(st.verification ?? null);
      setTools(st.tools ?? []);
      setResolvedProfile(st.profile ?? null);
      setError(st.error ?? "");
      setPhase(st.status === "done" ? "done" : st.status === "running" ? "running" : "error");
    } catch (e) {
      if (!controller.signal.aborted) {
        setError(e instanceof Error ? e.message : String(e));
      }
    }
  };

  const downloadMarkdown = () => {
    if (!reportMd) return;
    const blob = new Blob([reportMd], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `metasearch-research-${(jobId || "report").slice(0, 8)}.md`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  // Ref mirror so the stream-closed fallback can read the latest phase.
  const phaseRef = useRef<Phase>("idle");
  useEffect(() => {
    phaseRef.current = phase;
  }, [phase]);

  /** Poll the job status; only a TERMINAL status settles the phase — a
   * still-running job after a dropped stream is re-attached to SSE (the
   * backend replays its event buffer) up to `reattachLeft` times. */
  const finalize = async (id: string, signal: AbortSignal, reattachLeft = 0, expectDone = false) => {
    // Audit round 9: the done event and the stream-close fallback can both
    // call finalize — lock synchronously before awaiting so only one runs.
    if (settledRef.current) return;
    settledRef.current = true;
    try {
      let status: ResearchStatus | null = null;
      // After a "done" SSE event the DB status can lag a beat behind the event
      // bus — poll a few times before concluding the job is stuck.
      for (let attempt = 0; attempt < (expectDone ? 3 : 1); attempt++) {
        status = await getResearch(id, signal);
        if (status.status === "done" || status.status === "failed" || status.error) break;
        if (attempt < (expectDone ? 2 : 0)) await new Promise((r) => setTimeout(r, 400));
      }
      status = status!;
      if (status.status === "done") {
        setReportMd(status.report_md ?? "");
        reportRef.current = status.report_md ?? "";
        setCitations(status.citations ?? []);
        setLearnings(status.learnings ?? []);
        setOutline(status.outline ?? []);
        setConflicts(status.conflicts ?? []);
        setVerification(status.verification ?? null);
        setTools(status.tools ?? []);
        setResolvedProfile(status.profile ?? null);
        setStage("");
        setError(""); // a transient poll failure must not shadow the report
        setPhase("done");
        refreshHistory();
        return;
      }
      if (status.status === "failed" || status.status === "cancelled" || status.error) {
        setError(status.error || "Job nghiên cứu thất bại");
        setPhase("error");
        refreshHistory();
        return;
      }
      // Still queued/running: release lock before re-attaching or stopping.
      if (reattachLeft > 0 && !signal.aborted) {
        settledRef.current = false;
        await attachStream(id, signal, reattachLeft - 1);
        return;
      }
      setStage(`Job vẫn đang chạy (${status.status}) — mở lại tab để tiếp tục theo dõi.`);
      setPhase("error");
    } catch (e) {
      if (!signal.aborted) {
        setError(e instanceof Error ? e.message : String(e));
        setPhase("error");
      }
    }
  };

  /** Attach (or re-attach) to the job's SSE stream; resolves when it closes.
   * The backend replays its whole event buffer from seq 0 on every attach, so
   * the report accumulator MUST reset here — otherwise tokens duplicate. */
  const attachStream = async (id: string, signal: AbortSignal, reattachLeft: number) => {
    reportRef.current = "";
    streamDonePromiseRef.current = null;
    let streamErrored = false;
    try {
      await streamSse(
      researchEventsUrl(id),
      {
        onEvent: (name, dataJson) => {
          if (signal.aborted) return;
          if (name === "status") {
            try {
              const d = JSON.parse(dataJson) as { stage?: string; detail?: string };
              setStage([d.stage, d.detail].filter(Boolean).join(" — "));
            } catch {
              /* partial event */
            }
          } else if (name === "outline") {
            try {
              const d = JSON.parse(dataJson) as { sections?: string[]; profile?: string };
              if (Array.isArray(d.sections)) setOutline(d.sections);
              if (d.profile) setResolvedProfile(d.profile);
            } catch {
              /* ignore */
            }
          } else if (name === "tool_results") {
            try {
              const d = JSON.parse(dataJson) as { tools?: ResearchToolSummary[] };
              if (Array.isArray(d.tools)) setTools(d.tools);
            } catch {
              /* ignore */
            }
          } else if (name === "verification") {
            try {
              setVerification(JSON.parse(dataJson) as ResearchVerification);
            } catch {
              /* ignore */
            }
          } else if (name === "report_token") {
            try {
              const d = JSON.parse(dataJson) as { t?: string };
              if (d.t) {
                reportRef.current += d.t;
                if (rafRef.current === null) {
                  rafRef.current = requestAnimationFrame(() => {
                    rafRef.current = null;
                    setReportMd(reportRef.current);
                  });
                }
              }
            } catch {
              /* ignore */
            }
          } else if (name === "error") {
            streamErrored = true;
            settledRef.current = true;
            try {
              const d = JSON.parse(dataJson) as { message?: string };
              setError(d.message ?? "Job lỗi không rõ nguyên nhân");
            } catch {
              setError("Job lỗi không rõ nguyên nhân");
            }
            setPhase("error");
          } else if (name === "done") {
            streamDonePromiseRef.current = finalize(id, signal, 0, true);
          }
        },
        onError: (err) => {
          if (signal.aborted) return;
          streamErrored = true;
          settledRef.current = true;
          setError(err instanceof Error ? err.message : String(err));
          setPhase("error");
        },
      },
      signal
      );
    } catch {
      // A dropped connection (TCP reset mid-read) must not kill the panel:
      // fall through to the re-attach/finalize path unless we aborted on purpose.
      if (signal.aborted) return;
      setStage("Mất kết nối — đang thử kết nối lại...");
    }
    if (streamDonePromiseRef.current) {
      await streamDonePromiseRef.current;
    } else if (!signal.aborted && !streamErrored && !settledRef.current && phaseRef.current === "running") {
      await finalize(id, signal, reattachLeft);
    }
  };

  const sections = splitReportSections(reportMd);
  useEffect(() => {
    sectionsRef.current = sections;
  }, [sections]);

  return (
    <section className="card">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void start();
        }}
      >
        <div className="search-row">
          <input
            type="text"
            placeholder="Câu hỏi nghiên cứu sâu (VD: đánh giá thị trường thép VN 2026, so sánh ngân hàng niêm yết...)"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            disabled={phase === "running"}
          />
          <button type="submit" disabled={phase === "running" || !query.trim()}>
            {phase === "running" ? "Đang nghiên cứu..." : "Bắt đầu nghiên cứu"}
          </button>
        </div>
        <div className="research-controls">
          <label>
            Độ sâu:{" "}
            <select value={depth} onChange={(e) => setDepth(Number(e.target.value))} disabled={phase === "running"}>
              {DEPTHS.map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
          </label>
          <label>
            Độ rộng:{" "}
            <select value={breadth} onChange={(e) => setBreadth(Number(e.target.value))} disabled={phase === "running"}>
              {BREADTHS.map((b) => (
                <option key={b} value={b}>
                  {b}
                </option>
              ))}
            </select>
          </label>
          <label>
            Ngân sách thời gian:{" "}
            <select value={maxTimeS} onChange={(e) => setMaxTimeS(Number(e.target.value))} disabled={phase === "running"}>
              {TIME_BUDGETS.map((t) => (
                <option key={t} value={t}>
                  {t}s
                </option>
              ))}
            </select>
          </label>
          <label>
            Chế độ:{" "}
            <select value={profile} onChange={(e) => setProfile(e.target.value)} disabled={phase === "running"}>
              {PROFILES.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </label>
          <label>
            Space:{" "}
            <input
              type="text"
              placeholder="(tùy chọn)"
              value={space}
              onChange={(e) => setSpace(e.target.value)}
              disabled={phase === "running"}
              style={{ width: "8rem" }}
            />
          </label>
          {history.length > 0 && (
            <label>
              Lịch sử:{" "}
              <select
                value={selectedHistoryId}
                onChange={(e) => void loadHistoricalJob(e.target.value)}
              >
                <option value="">-- Chọn báo cáo --</option>
                {history.map((h) => (
                  <option key={h.research_id} value={h.research_id}>
                    [{h.status}] {h.query.slice(0, 40)}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
      </form>

      {phase === "running" && (
        <div className="card status">
          <div className="spinner" />
          <span>{stage || "Đang nghiên cứu..."}</span>
          {jobId && (
            <button
              type="button"
              className="tts-btn"
              onClick={() => void handleCancel()}
              style={{ marginLeft: "auto" }}
            >
              ⏹ Hủy
            </button>
          )}
        </div>
      )}
      {error && <div className="card error">{error}</div>}
      {jobId && phase !== "idle" && (
        <p className="research-job">
          Job: <code>{jobId}</code>
          {resolvedProfile ? ` · ${resolvedProfile}` : ""}
          {phase === "done" ? " · hoàn tất" : ""}
        </p>
      )}

      {(outline.length > 0 || tools.length > 0 || verification) && (
        <div className="card status" style={{ flexWrap: "wrap", gap: "0.5rem" }}>
          {tools.map((t, idx) => (
            <span key={`${t.tool}-${idx}`} className="chip">
              🔧 {t.tool}
            </span>
          ))}
          {outline.length > 0 && (
            <span className="chip">📐 Outline: {outline.length} mục</span>
          )}
          {verification && (
            <span className="chip">
              ✓ Fact: coverage {Math.round(verification.citation_coverage * 100)}% · numeric fidelity{" "}
              {Math.round(verification.numeric_fidelity * 100)}%
              {verification.unsupported_markers.length > 0
                ? ` · ⚠ ${verification.unsupported_markers.length} unsupported`
                : ""}
            </span>
          )}
        </div>
      )}

      {reportMd && (
        <article className="card answer">
          <header className="answer-header">
            <h2>Báo cáo nghiên cứu</h2>
            <span className="answer-header-actions">
              <button
                type="button"
                className="tts-btn"
                onClick={downloadMarkdown}
                title="Tải báo cáo Markdown (.md)"
              >
                ⬇ Tải .md
              </button>
            {(ttsReady || vsEnabled) && (
              <>
                {playingSection === null ? (
                  <button
                    type="button"
                    className="tts-btn"
                    onClick={() => speakSectionAt(0)}
                    aria-label="Nghe báo cáo"
                    title="Nghe báo cáo theo từng phần (giọng đọc tiếng Việt)"
                  >
                    🔊 Nghe báo cáo
                  </button>
                ) : (
                  <button type="button" className="tts-btn speaking" onClick={stopSpeaking} aria-label="Dừng đọc">
                    ⏹ Dừng đọc
                  </button>
                )}
                {playingSection !== null && (
                  <>
                    <button
                      type="button"
                      className="tts-btn"
                      onClick={() => speakSectionAt(playingSection - 1)}
                      aria-label="Phần trước"
                      disabled={playingSection - 1 < 0}
                    >
                      ⏮ Phần trước
                    </button>
                    <button
                      type="button"
                      className="tts-btn"
                      onClick={() => speakSectionAt(playingSection + 1)}
                      aria-label="Phần tiếp theo"
                      disabled={playingSection + 1 >= sections.length}
                    >
                      ⏭ Phần tiếp
                    </button>
                  </>
                )}
              </>
            )}
            </span>
          </header>

          {sections.length > 1 && (
            <div className="research-sections" aria-label="Danh sách phần của báo cáo">
              {sections.map((s, i) => (
                <button
                  key={`${i}-${s.title}`}
                  type="button"
                  className={`chip chip-toggle ${playingSection === i ? "on" : ""}`}
                  onClick={() => speakSectionAt(i)}
                  aria-pressed={playingSection === i}
                  title="Nghe phần này"
                >
                  {i + 1}. {s.title}
                </button>
              ))}
            </div>
          )}

          <AnswerMarkdown text={reportMd} citationByUrl={citationByUrl} />

          {citations.length > 0 && (
            <div className="citations">
              <h3>Nguồn trích dẫn ({citations.length})</h3>
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

          {(learnings.length > 0 || conflicts.length > 0) && (
            <details className="research-learnings">
              <summary>
                <strong>
                  Fact Ledger & Xung đột ({learnings.length} kết luận · {conflicts.length} mâu thuẫn)
                </strong>
              </summary>
              {learnings.length > 0 && (
                <ul>
                  {learnings.map((l, i) => (
                    <li key={i}>{l}</li>
                  ))}
                </ul>
              )}
              {conflicts.length > 0 && (
                <ul>
                  {conflicts.map((cf, i) => (
                    <li key={`cf-${i}`}>
                      <strong>{cf.topic}</strong>: {cf.claim_a}{" "}
                      {cf.citation_a ? `[${cf.citation_a}]` : ""} vs {cf.claim_b}{" "}
                      {cf.citation_b ? `[${cf.citation_b}]` : ""}
                    </li>
                  ))}
                </ul>
              )}
            </details>
          )}
        </article>
      )}
    </section>
  );
}

function buildCitationMap(citations: Citation[]): Map<number, Citation> {
  return new Map(citations.map((c) => [c.n, c]));
}

