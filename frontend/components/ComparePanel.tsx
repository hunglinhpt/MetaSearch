"use client";

/**
 * Compare tab (docs/18 B1/B2): POST /v1/fa/compare renders a metrics x
 * symbols table (every cell carries backend provenance; validated cells are
 * marked), with read-aloud via the preferred TTS engine, plus the OHLCV
 * candlestick chart (B2) for any selected symbol.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  faCompare,
  safeHref,
  ttsCapabilities,
  type FaCompareResponse,
} from "../lib/api";
import {
  getPreferredEngine,
  ttsIsSupported,
  ttsSpeak,
  ttsSpeakRemote,
  ttsStop,
  type TtsHandle,
} from "../lib/tts";
import { buildCompareSpeech, formatField, METRICS, parseSymbolsInput } from "../lib/faTable";
import ChartPanel from "./ChartPanel";

export default function ComparePanel() {
  const [input, setInput] = useState("");
  const [data, setData] = useState<FaCompareResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [chartSymbol, setChartSymbol] = useState<string>("");
  const [ttsReady, setTtsReady] = useState(false);
  const [vsEnabled, setVsEnabled] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const ttsRef = useRef<TtsHandle | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    setTtsReady(ttsIsSupported());
    ttsCapabilities()
      .then((c) => setVsEnabled(Boolean(c.voicestudio.enabled)))
      .catch(() => setVsEnabled(false));
    return () => {
      abortRef.current?.abort();
      ttsRef.current?.cancel();
      ttsStop();
    };
  }, []);

  const run = useCallback(async () => {
    const symbols = parseSymbolsInput(input);
    if (!symbols.length) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    setError("");
    ttsRef.current?.cancel();
    ttsStop();
    setSpeaking(false);
    try {
      const resp = await faCompare(symbols, controller.signal);
      if (controller.signal.aborted) return;
      setData(resp);
      setChartSymbol(resp.results[0]?.symbol ?? "");
    } catch (e) {
      if (controller.signal.aborted) return;
      setError(e instanceof Error ? e.message : String(e));
      setData(null);
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [input]);

  const speakTable = useCallback(() => {
    if (!data?.results.length) return;
    if (speaking) {
      ttsRef.current?.cancel();
      ttsStop();
      setSpeaking(false);
      return;
    }
    const text = buildCompareSpeech(data.results);
    const cbs = {
      onStart: () => setSpeaking(true),
      onEnd: () => setSpeaking(false),
      onError: () => setSpeaking(false),
      fallbackToWebSpeech: true,
    };
    ttsRef.current =
      getPreferredEngine() === "voicestudio" && vsEnabled
        ? ttsSpeakRemote(text, cbs)
        : ttsSpeak(text, cbs);
  }, [data, speaking, vsEnabled]);

  return (
    <section className="card">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void run();
        }}
      >
        <div className="search-row">
          <input
            type="text"
            placeholder="Nhập 2-5 mã, cách nhau bởi dấu phẩy (VD: VCB, FPT, HPG)"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            disabled={loading}
          />
          <button type="submit" disabled={loading || !parseSymbolsInput(input).length}>
            {loading ? "Đang so sánh..." : "So sánh"}
          </button>
          {data && (ttsReady || vsEnabled) && (
            <button
              type="button"
              className={`tts-btn ${speaking ? "speaking" : ""}`}
              onClick={speakTable}
              aria-pressed={speaking}
              aria-label={speaking ? "Dừng đọc bảng" : "Nghe đọc bảng so sánh"}
              title="Đọc bảng so sánh bằng giọng tiếng Việt"
            >
              {speaking ? "⏹" : "🔊"}
            </button>
          )}
        </div>
      </form>

      {error && <div className="card error">{error}</div>}

      {data && (
        <>
          <div className="md-table-wrap">
            <table className="md-table compare-table">
              <thead>
                <tr>
                  <th>Chỉ số</th>
                  {data.results.map((r) => (
                    <th key={r.symbol}>
                      {r.symbol}
                      {r.exchange && <span className="muted"> ({r.exchange})</span>}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {METRICS.map((m) => (
                  <tr key={m.key}>
                    <td>{m.label}</td>
                    {data.results.map((r) => {
                      const f = r.fields[m.key];
                      return (
                        <td key={r.symbol}>
                          {formatField(m, f?.value)}
                          {f && (
                            <span
                              className="field-src"
                              title={`nguồn: ${f.source}${f.as_of ? ` · mốc: ${f.as_of.slice(0, 10)}` : ""}`}
                            >
                              {f.validated ? "đa nguồn" : f.source}
                            </span>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="compare-footer">
            {data.results.map((r) => (
              <p key={r.symbol} className="muted small">
                {r.symbol}: nguồn {r.sources_used.join(", ") || "không có"}
                {r.as_of && <> · dữ liệu mốc {r.as_of.slice(0, 10)}</>}
                {r.source_urls[0] && (
                  <>
                    {" "}
                    · <a href={safeHref(r.source_urls[0])} target="_blank" rel="noreferrer">nguồn gốc</a>
                  </>
                )}
                {r.missing_fields.length > 0 && <> · thiếu {r.missing_fields.length} trường</>}
              </p>
            ))}
          </div>

          {data.results.length > 0 && (
            <div className="chart-picker">
              <span className="muted">Biểu đồ nến:</span>
              {data.results.map((r) => (
                <button
                  key={r.symbol}
                  type="button"
                  className={`chip chip-toggle ${chartSymbol === r.symbol ? "on" : ""}`}
                  onClick={() => setChartSymbol(r.symbol)}
                >
                  {r.symbol}
                </button>
              ))}
            </div>
          )}
          {chartSymbol && <ChartPanel symbol={chartSymbol} />}
        </>
      )}
    </section>
  );
}
