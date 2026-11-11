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

  useEffect(() => {
    setTtsReady(ttsIsSupported());
    ttsCapabilities()
      .then((c) => setVsEnabled(Boolean(c.voicestudio.enabled)))
      .catch(() => setVsEnabled(false));
    return () => {
      abortRef.current?.abort();
      ttsHandleRef.current?.cancel();
      ttsStop();
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
      await streamSse(
        researchEventsUrl(created.research_id),
        {
          onEvent: (name, dataJson) => {
            if (controller.signal.aborted) return;
            if (name === "status") {
              try {
                const d = JSON.parse(dataJson) as { stage?: string; detail?: string };
                setStage([d.stage, d.detail].filter(Boolean).join(" — "));
              } catch {
                /* partial event: ignore, next one carries the payload */
              }
            } else if (name === "report_token") {
              try {
                const d = JSON.parse(dataJson) as { t?: string };
                if (d.t) {
                  reportRef.current += d.t;
                  setReportMd(reportRef.current);
                }
              } catch {
                /* ignore malformed token */
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
              void finalize(created.research_id, controller.signal);
            }
          },
          onError: (err) => {
            if (controller.signal.aborted) return;
            setError(err instanceof Error ? err.message : String(err));
            setPhase("error");
          },
        },
        controller.signal
      );
      // Stream closed: if no done event arrived but a job id exists, poll once.
      if (!controller.signal.aborted && phaseRef.current === "running" && created.research_id) {
        await finalize(created.research_id, controller.signal);
      }
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

  const finalize = async (id: string, signal: AbortSignal) => {
    try {
      const status: ResearchStatus = await getResearch(id, signal);
      setReportMd(status.report_md ?? "");
      reportRef.current = status.report_md ?? "";
      setCitations(status.citations ?? []);
      setLearnings(status.learnings ?? []);
      if (status.error) {
        setError(status.error);
        setPhase("error");
      } else {
        setStage("");
        setPhase("done");
      }
    } catch (e) {
      if (!signal.aborted) {
        setError(e instanceof Error ? e.message : String(e));
        setPhase("error");
      }
    }
  };

  const sections = splitReportSections(reportMd);
  sectionsRef.current = sections;

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
                  <button
                    type="button"
                    className="tts-btn"
                    onClick={() => speakSectionAt(playingSection + 1)}
                    aria-label="Phần tiếp theo"
                    disabled={playingSection + 1 >= sections.length}
                  >
                    ⏭ Phần tiếp
                  </button>
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

