"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  API_BASE_URL,
  ApiError,
  AnswerMeta,
  Citation,
  EngineDone,
  EngineInfo,
  SearchHit,
  SourceItem,
  listEngines,
  search,
  streamAnswer,
} from "../lib/api";

type Mode = "answer" | "raw";

export default function HomePage() {
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

  useEffect(() => {
    const controller = new AbortController();
    listEngines(controller.signal)
      .then((engines) =>
        setEngineOptions(engines.filter((e) => e.enabled && e.adapter_ready))
      )
      .catch(() => setEngineOptions([])); // filter is optional sugar
    return () => controller.abort();
  }, []);

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
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        if (err instanceof ApiError) {
          setError(`[${err.code}] ${err.message}`);
        } else {
          setError((err as Error).message);
        }
      }
    } finally {
      setBusy(false);
      setStage("");
    }
  }, [busy, mode, query, reset, selectedEngines]);

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    void run();
  };

  const citationByUrl = new Map(citations.map((c) => [c.n, c]));

  return (
    <main className="shell shell-wide">
      <h1>
        Meta<span className="accent">Search</span>
      </h1>
      <p className="tagline">
        Meta-search qua SearXNG (70+ engines) · trích xuất sạch · trả lời có trích dẫn
      </p>

      <form className="searchbar" onSubmit={submit}>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Nhập câu hỏi hoặc từ khoá…"
          autoFocus
          disabled={busy}
        />
        <div className="modes">
          <button
            type="button"
            className={mode === "answer" ? "active" : ""}
            onClick={() => setMode("answer")}
            disabled={busy}
          >
            ✨ Trả lời AI
          </button>
          <button
            type="button"
            className={mode === "raw" ? "active" : ""}
            onClick={() => setMode("raw")}
            disabled={busy}
          >
            🔎 Tìm kiếm
          </button>
        </div>
        <button className="go" type="submit" disabled={busy || !query.trim()}>
          {busy ? "…" : "Chạy"}
        </button>
      </form>

      {engineOptions.length > 0 && (
        <div className="engines engines-filter">
          {engineOptions.map((e) => (
            <button
              key={e.id}
              type="button"
              className={`chip chip-toggle${selectedEngines.includes(e.id) ? " on" : ""}`}
              onClick={() => toggleEngine(e.id)}
              disabled={busy}
              title={e.name}
            >
              {e.id}
            </button>
          ))}
        </div>
      )}

      {stage && <p className="stage">{stage}</p>}
      {engines.length > 0 && (
        <div className="engines">
          {engines.map((e) => (
            <span key={e.engine} className="chip">
              {e.engine}: {e.results} kết quả · {e.ms} ms
            </span>
          ))}
        </div>
      )}
      {error && <p className="error">⚠ {error}</p>}

      <div className="layout">
        <section className="col-main">
          {mode === "answer" && (answerMd || meta) && (
            <article className="card answer">
              <AnswerMarkdown text={answerMd} citationByUrl={citationByUrl} />
              {meta && (
                <footer className="answer-meta">
                  {meta.cached ? "đã cache" : "mới"} · {meta.model} · {meta.latency_ms} ms
                </footer>
              )}
              {citations.length > 0 && (
                <div className="citations">
                  <h3>Trích dẫn</h3>
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
            <h3>Nguồn ({sources.length})</h3>
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

      <footer className="footer">
        API: <code>{API_BASE_URL}/docs</code>
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
