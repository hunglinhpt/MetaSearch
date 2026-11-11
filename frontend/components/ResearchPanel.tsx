"use client";

/**
 * Deep Research tab (docs/18 §A2): consumes the FR-9 backend that previously
 * had no UI — POST /v1/research → SSE /events (status + report_token) →
 * final GET for citations/learnings — and reads the finished report aloud
 * SECTION BY SECTION (playlist with next/stop) through the preferred TTS
 * engine (Web Speech default, VoiceStudio when configured — docs/17).
 */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  createResearch,
  getResearch,
  researchEventsUrl,
  safeHref,
  streamSse,
  ttsCapabilities,
  type Citation,
  type ResearchStatus,
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

export default function ResearchPanel() {
  const [query, setQuery] = useState("");
  const [depth, setDepth] = useState<number>(3);
  const [breadth, setBreadth] = useState<number>(4);
  const [maxTimeS, setMaxTimeS] = useState<number>(180);
  const [phase, setPhase] = useState<Phase>("idle");
  const [stage, setStage] = useState("");
  const [reportMd, setReportMd] = useState("");
  const [citations, setCitations] = useState<Citation[]>([]);
  const [learnings, setLearnings] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);

  // Section playlist state.
  const [playingSection, setPlayingSection] = useState<number | null>(null);
  const [ttsReady, setTtsReady] = useState(false);
  const [vsEnabled, setVsEnabled] = useState(false);
  const ttsHandleRef = useRef<TtsHandle | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const sectionsRef = useRef<{ title: string; text: string }[]>([]);
  const reportRef = useRef("");
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    setTtsReady(ttsIsSupported());
    ttsCapabilities()
      .then((c) => setVsEnabled(Boolean(c.voicestudio.enabled)))
      .catch(() => setVsEnabled(false));
    return () => {
      abortRef.current?.abort();
      ttsHandleRef.current?.cancel();
      ttsStop();
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
  }, []);

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
    setError("");
    setJobId(null);
    stopSpeaking();
    try {
      const created = await createResearch(
        { query: q, breadth, depth, max_time_s: maxTimeS },
        controller.signal
      );
      setJobId(created.research_id);
      setStage("Đang lên kế hoạch nghiên cứu...");
      // attachStream re-attaches on a dropped connection (the backend replays
      // its event buffer) and only settles on a TERMINAL job status.
      await attachStream(created.research_id, controller.signal, 3);
    } catch (e) {
      if (controller.signal.aborted) return;
      setError(e instanceof Error ? e.message : String(e));
      setPhase("error");
    }
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
    try {
      let status: ResearchStatus | null = null;
      // After a "done" SSE event the DB status can lag a beat behind the event
      // bus — poll a few times before concluding the job is stuck.
      for (let attempt = 0; attempt < (expectDone ? 3 : 1); attempt++) {
        status = await getResearch(id, signal);
        if (status.status === "done" || status.status === "failed" || status.error) break;
        if (attempt < 2) await new Promise((r) => setTimeout(r, 400));
      }
      status = status!;
      if (status.status === "done") {
        setReportMd(status.report_md ?? "");
        reportRef.current = status.report_md ?? "";
        setCitations(status.citations ?? []);
        setLearnings(status.learnings ?? []);
        setStage("");
        setPhase("done");
        return;
      }
      if (status.status === "failed" || status.error) {
        setError(status.error || "Job nghiên cứu thất bại");
        setPhase("error");
        return;
      }
      // Still queued/running: never mark "done" with an empty report.
      if (reattachLeft > 0 && !signal.aborted) {
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
            try {
              const d = JSON.parse(dataJson) as { message?: string };
              setError(d.message ?? "Job lỗi không rõ nguyên nhân");
            } catch {
              setError("Job lỗi không rõ nguyên nhân");
            }
            setPhase("error");
          } else if (name === "done") {
            void finalize(id, signal, 0, true);
          }
        },
        onError: (err) => {
          if (signal.aborted) return;
          setError(err instanceof Error ? err.message : String(err));
          setPhase("error");
        },
      },
      signal
      );
    } catch (e) {
      // A dropped connection (TCP reset mid-read) must not kill the panel:
      // fall through to the re-attach/finalize path unless we aborted on purpose.
      if (signal.aborted) return;
      setStage("Mất kết nối — đang thử kết nối lại...");
    }
    if (!signal.aborted && phaseRef.current === "running") {
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
        </div>
      </form>

      {phase === "running" && (
        <div className="card status">
          <div className="spinner" />
          <span>{stage || "Đang nghiên cứu..."}</span>
        </div>
      )}
      {error && <div className="card error">{error}</div>}
      {jobId && phase !== "idle" && (
        <p className="research-job">Job: <code>{jobId}</code>{phase === "done" ? " · hoàn tất" : ""}</p>
      )}

      {reportMd && (
        <article className="card answer">
          <header className="answer-header">
            <h2>Báo cáo nghiên cứu</h2>
            {(ttsReady || vsEnabled) && (
              <span className="answer-header-actions">
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
              </span>
            )}
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

          <AnswerMarkdown text={reportMd} citationByUrl={buildCitationMap(citations)} />

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

          {learnings.length > 0 && (
            <div className="research-learnings">
              <h3>Kết luận rút ra ({learnings.length})</h3>
              <ul>
                {learnings.map((l, i) => (
                  <li key={i}>{l}</li>
                ))}
              </ul>
            </div>
          )}
        </article>
      )}
    </section>
  );
}

function buildCitationMap(citations: Citation[]): Map<number, Citation> {
  return new Map(citations.map((c) => [c.n, c]));
}

