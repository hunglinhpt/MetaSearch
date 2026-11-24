"use client";

/**
 * Trust & Insight tab (docs/34, v4.5.0):
 *  - "Kiểm chứng": POST /v1/verify — verdict + verbatim evidence quotes, each
 *    with its source tier and timestamps (nothing is paraphrased client-side).
 *  - "Dòng thời gian": POST /v1/timeline — distinct dated events oldest ->
 *    newest + the coverage lens (tiers, countries, daily volume).
 * Colours come only from theme CSS variables (DESIGN.md); every href goes
 * through safeHref().
 */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  eventTimeline,
  safeHref,
  verifyClaim,
  type TimelineResponse,
  type VerifyResponse,
} from "../lib/api";
import {
  STANCE_LABEL,
  VERDICT_ICON,
  VERDICT_LABEL,
  claimIsValid,
  pct,
  stanceColorVar,
  topEntries,
  verdictColorVar,
  viDate,
  volumeBars,
} from "../lib/trust";

type Mode = "verify" | "timeline";

const TIER_LABEL: Record<string, string> = {
  official: "chính thống",
  primary: "nguồn gốc",
  research: "học thuật",
  reference: "tham khảo",
  aggregator: "tổng hợp",
  community: "cộng đồng",
  unknown: "chưa phân hạng",
  low_trust: "độ tin thấp",
};

export default function TrustPanel() {
  const [mode, setMode] = useState<Mode>("verify");
  const [claim, setClaim] = useState("");
  const [useLlm, setUseLlm] = useState(false);
  const [topic, setTopic] = useState("");
  const [days, setDays] = useState(30);
  const [verify, setVerify] = useState<VerifyResponse | null>(null);
  const [timeline, setTimeline] = useState<TimelineResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => () => abortRef.current?.abort(), []);

  const start = () => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setLoading(true);
    setError("");
    return ctrl;
  };

  const runVerify = useCallback(async () => {
    if (!claimIsValid(claim)) return;
    const ctrl = start();
    try {
      const data = await verifyClaim(claim, { useLlm }, ctrl.signal);
      if (!ctrl.signal.aborted) setVerify(data);
    } catch (e) {
      if (!ctrl.signal.aborted) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (abortRef.current === ctrl) setLoading(false);
    }
  }, [claim, useLlm]);

  const runTimeline = useCallback(async () => {
    if (topic.trim().length < 2) return;
    const ctrl = start();
    try {
      const data = await eventTimeline(topic.trim(), { days }, ctrl.signal);
      if (!ctrl.signal.aborted) setTimeline(data);
    } catch (e) {
      if (!ctrl.signal.aborted) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (abortRef.current === ctrl) setLoading(false);
    }
  }, [topic, days]);

  return (
    <section className="card trust-panel">
      <div className="trust-modes" role="tablist" aria-label="Chế độ kiểm chứng">
        <button
          type="button"
          role="tab"
          aria-selected={mode === "verify"}
          className={`chip chip-toggle ${mode === "verify" ? "on" : ""}`}
          onClick={() => setMode("verify")}
        >
          ✅ Kiểm chứng tuyên bố
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={mode === "timeline"}
          className={`chip chip-toggle ${mode === "timeline" ? "on" : ""}`}
          onClick={() => setMode("timeline")}
        >
          🗓️ Dòng thời gian sự kiện
        </button>
      </div>

      {mode === "verify" && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void runVerify();
          }}
        >
          <div className="search-row">
            <input
              type="text"
              placeholder="Nhập một tuyên bố cần kiểm chứng (VD: GDP Việt Nam năm 2024 tăng 7,09%)"
              value={claim}
              maxLength={500}
              onChange={(e) => setClaim(e.target.value)}
              disabled={loading}
              aria-label="Tuyên bố cần kiểm chứng"
            />
            <button type="submit" disabled={loading || !claimIsValid(claim)}>
              {loading ? "Đang kiểm chứng..." : "Kiểm chứng"}
            </button>
          </div>
          <label className="muted trust-opt">
            <input
              type="checkbox"
              checked={useLlm}
              onChange={(e) => setUseLlm(e.target.checked)}
              disabled={loading}
            />{" "}
            Nhờ LLM đánh giá lại lập trường (không thêm bằng chứng; lỗi → giữ đánh giá tất định)
          </label>
        </form>
      )}

      {mode === "timeline" && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void runTimeline();
          }}
        >
          <div className="search-row">
            <input
              type="text"
              placeholder="Chủ đề (VD: lãi suất Fed, FPT, bão Yagi)"
              value={topic}
              maxLength={300}
              onChange={(e) => setTopic(e.target.value)}
              disabled={loading}
              aria-label="Chủ đề dòng thời gian"
            />
            <select
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
              disabled={loading}
              aria-label="Cửa sổ thời gian"
            >
              <option value={7}>7 ngày</option>
              <option value={30}>30 ngày</option>
              <option value={90}>90 ngày</option>
              <option value={365}>365 ngày</option>
            </select>
            <button type="submit" disabled={loading || topic.trim().length < 2}>
              {loading ? "Đang dựng..." : "Dựng dòng thời gian"}
            </button>
          </div>
        </form>
      )}

      {error && <div className="card error">{error}</div>}

      {mode === "verify" && verify && <VerifyView data={verify} />}
      {mode === "timeline" && timeline && <TimelineView data={timeline} />}
    </section>
  );
}

function VerifyView({ data }: { data: VerifyResponse }) {
  return (
    <div className="trust-result">
      <div className="verdict-banner" style={{ borderLeftColor: verdictColorVar(data.verdict) }}>
        <div className="verdict-title" style={{ color: verdictColorVar(data.verdict) }}>
          {VERDICT_ICON[data.verdict]} {VERDICT_LABEL[data.verdict]}
        </div>
        <div className="muted">
          Độ tin cậy {pct(data.confidence)} · {data.independent_domains} tên miền độc lập ·{" "}
          {data.counts.supports ?? 0} ủng hộ / {data.counts.refutes ?? 0} bác bỏ /{" "}
          {data.counts.mentions ?? 0} chỉ nhắc tới · đánh giá:{" "}
          {data.judge === "llm" ? "LLM (trên bằng chứng có sẵn)" : "tất định"}
          {data.single_source && " · ⚠️ chỉ một nguồn"}
          {!data.complete && " · ⚠️ một phần nguồn không tải được"}
        </div>
      </div>
      {data.evidence.length === 0 && (
        <p className="muted">
          Không tìm thấy đoạn văn nào đủ liên quan để kết luận — hệ thống không đoán.
        </p>
      )}
      <ol className="evidence-list">
        {data.evidence.map((e) => (
          <li key={`${e.n}-${e.url}`} className="evidence-item">
            <div className="evidence-head">
              <span className="stance-badge" style={{ color: stanceColorVar(e.stance) }}>
                {STANCE_LABEL[e.stance]}
              </span>
              <a href={safeHref(e.url)} target="_blank" rel="noopener noreferrer">
                {e.title || e.domain}
              </a>
              <span className="muted">
                {" "}
                · {e.domain} ({TIER_LABEL[e.domain_tier] ?? e.domain_tier}) · liên quan{" "}
                {pct(e.relevance)}
                {e.published_at && ` · đăng ${viDate(e.published_at)}`}
              </span>
            </div>
            <blockquote className="evidence-quote">{e.quote}</blockquote>
            {(e.refute_cue || e.polarity_flip || e.hearsay || (e.numeric_match ?? 1) < 1) && (
              <div className="muted evidence-why">
                {e.refute_cue && `tín hiệu bác bỏ: “${e.refute_cue}” `}
                {e.polarity_flip && "· phủ định ngược chiều tuyên bố "}
                {e.hearsay && "· đoạn văn chỉ thuật lại tin đồn/tuyên bố, không khẳng định "}
                {typeof e.numeric_match === "number" &&
                  e.numeric_match < 1 &&
                  `· số liệu khớp ${pct(e.numeric_match)}`}
              </div>
            )}
          </li>
        ))}
      </ol>
      <p className="muted trust-method">Phương pháp: {data.method}</p>
    </div>
  );
}

function TimelineView({ data }: { data: TimelineResponse }) {
  const bars = volumeBars(data.coverage.volume_by_day);
  return (
    <div className="trust-result">
      <div className="muted">
        {data.events.length}/{data.events_total} sự kiện · {data.coverage.distinct_domains} tên
        miền · bỏ {data.undated_dropped} kết quả không có ngày đăng (không đoán ngày)
        {!data.complete && " · ⚠️ một phần nguồn lỗi"}
      </div>
      {data.coverage.volume_by_day.length > 0 && (
        <div className="volume-bars" role="img" aria-label="Số bài theo ngày">
          {data.coverage.volume_by_day.map((d, i) => (
            <span
              key={d.date}
              className="volume-bar"
              style={{ height: `${Math.max(4, bars[i])}%` }}
              title={`${viDate(d.date)}: ${d.count} bài`}
            />
          ))}
        </div>
      )}
      <div className="coverage-chips">
        {topEntries(data.coverage.by_domain_tier).map(([k, v]) => (
          <span key={`t-${k}`} className="chip">
            {TIER_LABEL[k] ?? k}: {v}
          </span>
        ))}
        {topEntries(data.coverage.by_country).map(([k, v]) => (
          <span key={`c-${k}`} className="chip">
            {k === "global" ? "🌐 quốc tế" : k === "unknown" ? "?" : k}: {v}
          </span>
        ))}
      </div>
      {data.events.length === 0 && (
        <p className="muted">Không có kết quả nào mang ngày đăng thật trong cửa sổ này.</p>
      )}
      <ol className="timeline-list">
        {data.events.map((ev) => (
          <li key={`${ev.first_reported_at}-${ev.title}`} className="timeline-item">
            <div className="timeline-date">{viDate(ev.first_reported_at)}</div>
            <div className="timeline-body">
              <div className="timeline-title">{ev.title}</div>
              {ev.summary && <div className="muted">{ev.summary}</div>}
              <div className="timeline-sources">
                {ev.sources.map((s) => (
                  <a
                    key={s.url}
                    href={safeHref(s.url)}
                    target="_blank"
                    rel="noopener noreferrer"
                    title={`${s.domain} · ${TIER_LABEL[s.domain_tier] ?? s.domain_tier}`}
                  >
                    {s.domain}
                  </a>
                ))}
                {ev.sources_count > ev.sources.length && (
                  <span className="muted"> +{ev.sources_count - ev.sources.length}</span>
                )}
              </div>
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}
