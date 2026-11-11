"use client";

/**
 * News tab (docs/16-web-ui-news-tts.md): a Vietnamese news feed across
 * economics / finance / business / technology, backed by /v1/search with the
 * news engines (google_news) and freshness windows, plus Vietnamese TTS
 * (Web Speech API — lib/tts.ts) so the feed can be *listened* to, not only read.
 *
 * Freshness: google_news ignores the server-side freshness_days param, so the
 * client appends Google News' `when:{n}d` operator to the query — the UI only
 * ever claims a window the engine actually enforces (SA audit finding).
 *
 * Data integrity: every item renders source domain + published time straight
 * from the backend payload (published_at/as_of, never guessed client-side);
 * `complete === false` renders an explicit partial-results notice.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { safeHref, SearchHit, search } from "../lib/api";
import {
  TtsHandle,
  ttsHasVietnameseVoice,
  ttsIsSupported,
  ttsOnVoicesChanged,
  ttsSpeak,
  ttsStop,
} from "../lib/tts";

export type NewsCategory = {
  id: string;
  label: string;
  icon: string;
  query: string;
  engines: string[];
  freshnessDays: number;
};

export const NEWS_CATEGORIES: NewsCategory[] = [
  {
    id: "macro",
    label: "Kinh tế vĩ mô",
    icon: "🏛️",
    query: "tin tức kinh tế vĩ mô Việt Nam GDP CPI",
    engines: ["google_news"],
    freshnessDays: 3,
  },
  {
    id: "finance",
    label: "Tài chính – Chứng khoán",
    icon: "📈",
    query: "thị trường chứng khoán Việt Nam VN-Index tin tức",
    engines: ["google_news"],
    freshnessDays: 2,
  },
  {
    id: "business",
    label: "Kinh doanh – Doanh nghiệp",
    icon: "🏢",
    query: "tin kinh doanh doanh nghiệp Việt Nam",
    engines: ["google_news"],
    freshnessDays: 3,
  },
  {
    id: "tech",
    label: "Công nghệ – AI",
    icon: "💻",
    query: "tin tức công nghệ AI phần mềm",
    engines: ["google_news"],
    freshnessDays: 3,
  },
  {
    id: "banking",
    label: "Ngân hàng – Tiền tệ",
    icon: "🏦",
    query: "tin tức ngân hàng lãi suất chính sách tiền tệ",
    engines: ["google_news"],
    freshnessDays: 3,
  },
  {
    id: "world",
    label: "Kinh tế thế giới",
    icon: "🌍",
    query: "kinh tế thế giới thị trường toàn cầu tin tức",
    engines: ["google_news"],
    freshnessDays: 3,
  },
];

const NEWS_K = 15;
const READ_ALOUD_TOP_N = 8;

function fmtWhen(hit: SearchHit): string {
  const raw = hit.published_at ?? hit.as_of;
  if (!raw) return "không rõ thời điểm";
  const ts = Date.parse(raw);
  if (Number.isNaN(ts)) return raw;
  const diffMs = Date.now() - ts;
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return "vừa xong";
  if (mins < 60) return `${mins} phút trước`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours} giờ trước`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} ngày trước`;
  return new Date(ts).toISOString().slice(0, 10);
}

export default function NewsPanel() {
  const [activeCatId, setActiveCatId] = useState<string>("finance");
  const [customQuery, setCustomQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState<string | null>(null); // current feed query
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [complete, setComplete] = useState<boolean | undefined>(undefined);
  const [speakingId, setSpeakingId] = useState<string | null>(null);
  const [ttsReady, setTtsReady] = useState(false);
  const [hasViVoice, setHasViVoice] = useState(true);
  const ttsHandleRef = useRef<TtsHandle | null>(null);
  // Request-id + abort guard: a slow in-flight response can never overwrite a
  // newer category's feed, and a hung backend cannot leave the tab disabled.
  const reqIdRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    // Voices/SpeechSynthesis exist only client-side — gate after mount to
    // avoid SSR/hydration mismatch.
    setTtsReady(ttsIsSupported());
    setHasViVoice(ttsHasVietnameseVoice());
    // The voice list populates asynchronously (Chrome/Safari): re-check on
    // every voiceschanged so the vi-voice hint cannot stick at the wrong value.
    const offVoices = ttsOnVoicesChanged(() => setHasViVoice(ttsHasVietnameseVoice()));
    return () => {
      offVoices();
      ttsStop();
      ttsHandleRef.current?.cancel();
      abortRef.current?.abort();
    };
  }, []);

  const activeCat = NEWS_CATEGORIES.find((c) => c.id === activeCatId) ?? NEWS_CATEGORIES[0];

  const fetchNews = useCallback(async (cat: NewsCategory, overrideQuery?: string) => {
    // Invalidate any in-flight response and abort its request.
    const myId = ++reqIdRef.current;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    // Stale = superseded by a newer request OR aborted (unmount / 30s timeout).
    const stale = () => reqIdRef.current !== myId || controller.signal.aborted;
    // A hung backend must not leave the tab disabled forever (server caps at
    // 60s; this client-side belt-and-braces aborts at 30s).
    const timer = setTimeout(() => controller.abort(), 30_000);

    setLoading(true);
    setError("");
    ttsHandleRef.current?.cancel();
    ttsStop();
    setSpeakingId(null);
    try {
      const base = (overrideQuery ?? cat.query).trim() || cat.query;
      // google_news ignores freshness_days server-side; `when:Nd` is the
      // operator its RSS feed actually honors (docs/16 §1). Never append a
      // second operator when the user's own query already carries one.
      const q =
        cat.engines.includes("google_news") && !/when:\d+[dwm]/i.test(base)
          ? `${base} when:${cat.freshnessDays}d`
          : base;
      setAppliedQuery(q);
      const resp = await search(q, {
        engines: cat.engines,
        categories: ["news"],
        freshnessDays: cat.freshnessDays,
        k: NEWS_K,
        signal: controller.signal,
      });
      if (stale()) return;
      setHits(resp.results ?? []);
      setComplete(resp.complete);
    } catch (e) {
      if (stale()) return;
      setHits([]);
      setComplete(undefined);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      clearTimeout(timer);
      if (!stale()) setLoading(false);
    }
  }, []);

  // Load the default category when the tab opens (parent mounts this panel
  // only while its tab is active).
  useEffect(() => {
    void fetchNews(activeCat);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const stopSpeaking = () => {
    ttsHandleRef.current?.cancel();
    ttsHandleRef.current = null;
    ttsStop();
    setSpeakingId(null);
  };

  const speakDigest = () => {
    stopSpeaking();
    const items = hits.slice(0, READ_ALOUD_TOP_N);
    if (!items.length) return;
    const text = [
      `Bản tin ${activeCat.label.toLowerCase()} từ MetaSearch.`,
      ...items.map((h, i) => {
        // Speech falls back to the domain (never a raw URL) when the title is
        // missing, and drops the leading "[Nguồn]" tag google_news prefixes.
        const title = h.title || h.domain;
        const snippet = (h.snippet ?? "").replace(/^\[[^\]]+\]\s*/, "");
        return `Tin ${i + 1}: ${title}. ${snippet ? `${snippet}.` : ""} Nguồn ${h.domain}.`;
      }),
    ].join(" ");
    ttsHandleRef.current = ttsSpeak(text, {
      onStart: () => setSpeakingId("__digest__"),
      onEnd: () => setSpeakingId(null),
      onError: () => setSpeakingId(null),
    });
  };

  const speakOne = (hit: SearchHit) => {
    stopSpeaking();
    const snippet = (hit.snippet ?? "").replace(/^\[[^\]]+\]\s*/, "");
    const text = `${hit.title || hit.domain}. ${snippet} Nguồn ${hit.domain}.`;
    ttsHandleRef.current = ttsSpeak(text, {
      onStart: () => setSpeakingId(hit.url),
      onEnd: () => setSpeakingId(null),
      onError: () => setSpeakingId(null),
    });
  };

  return (
    <section className="card">
      <div className="category-bar" style={{ flexWrap: "wrap", gap: "0.4rem" }}>
        {NEWS_CATEGORIES.map((cat) => (
          <button
            key={cat.id}
            type="button"
            className={`chip chip-toggle ${cat.id === activeCatId ? "on" : ""}`}
            aria-pressed={cat.id === activeCatId}
            onClick={() => {
              setActiveCatId(cat.id);
              setCustomQuery("");
              void fetchNews(cat);
            }}
            disabled={loading}
            title={`Tin ${cat.label.toLowerCase()} trong ${cat.freshnessDays} ngày gần nhất`}
          >
            {cat.icon} {cat.label}
          </button>
        ))}
      </div>

      <form
        className="search-row"
        onSubmit={(e) => {
          e.preventDefault();
          if (!customQuery.trim()) return;
          void fetchNews(activeCat, customQuery);
        }}
      >
        <input
          type="text"
          aria-label={`Truy vấn tin riêng trong mục ${activeCat.label}`}
          placeholder={`Truy vấn riêng trong mục ${activeCat.label} (VD: lãi suất NHNN, kết quả kinh doanh FPT...)`}
          value={customQuery}
          onChange={(e) => setCustomQuery(e.target.value)}
          disabled={loading}
        />
        <button type="submit" disabled={loading || !customQuery.trim()}>
          {loading ? "Đang tải..." : "Lọc tin"}
        </button>
      </form>

      <div className="news-toolbar">
        <span className="news-info">
          {appliedQuery ? (
            <>
              Truy vấn: <code>{appliedQuery}</code> · cửa sổ {activeCat.freshnessDays} ngày ·
              nguồn: {activeCat.engines.join(", ")}
            </>
          ) : (
            "Chọn một mục hoặc nhập truy vấn riêng."
          )}
        </span>
        {ttsReady && hits.length > 0 && (
          speakingId === "__digest__" ? (
            <button
              type="button"
              className="tts-btn"
              onClick={stopSpeaking}
              aria-label="Dừng đọc bản tin"
            >
              ⏹ Dừng đọc
            </button>
          ) : (
            <button
              type="button"
              className="tts-btn"
              onClick={speakDigest}
              aria-label="Đọc tin mới nhất"
              title={`Đọc ${Math.min(READ_ALOUD_TOP_N, hits.length)} tin mới nhất bằng giọng đọc tiếng Việt. Riêng tư: giọng online của Edge gửi nội dung đọc từ trình duyệt tới Microsoft; giọng nội bộ của OS thì giữ trên máy`}
            >
              🔊 Đọc tin mới nhất
            </button>
          )
        )}
        {!ttsReady && (
          <span className="news-tts-hint">
            🔇 Trình duyệt không hỗ trợ đọc máy (Web Speech API) — dùng Chrome/Edge để nghe tin.
          </span>
        )}
        {ttsReady && !hasViVoice && (
          <span className="news-tts-hint">
            ⚠️ Chưa có giọng tiếng Việt trên máy — tiếng Việt sẽ đọc bằng giọng mặc định.
            Cài giọng vi-VN (Edge online voices) để đọc chuẩn.
          </span>
        )}
      </div>

      {complete === false && (
        <p className="news-partial">
          ⚠️ Một phần engine báo lỗi — danh sách dưới đây có thể KHÔNG đầy đủ (không phải
          "không có tin").
        </p>
      )}
      {error && <p className="error">Lỗi tải tin: {error}</p>}

      {loading ? (
        <p className="news-loading">Đang tải tin...</p>
      ) : hits.length === 0 && !error ? (
        <p className="news-loading">
          Chưa có tin nào trong cửa sổ thời gian này cho truy vấn hiện tại (kết quả trống là
          kết luận thật, không phải lỗi).
        </p>
      ) : (
        <div className="news-feed">
          {hits.map((hit) => (
            <article className="news-item" key={`${hit.url}#${hit.rank}`}>
              <div className="news-item-head">
                <a
                  className="news-title"
                  href={safeHref(hit.url)}
                  target="_blank"
                  rel="noreferrer"
                >
                  {hit.title || hit.url}
                </a>
                {ttsReady && (
                  speakingId === hit.url ? (
                    <button
                      type="button"
                      className="tts-btn"
                      onClick={stopSpeaking}
                      aria-label="Dừng đọc"
                      title="Dừng đọc"
                    >
                      ⏹
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="tts-btn"
                      onClick={() => speakOne(hit)}
                      aria-label="Đọc tin này"
                      title="Đọc tin này bằng giọng tiếng Việt"
                    >
                      🔊
                    </button>
                  )
                )}
              </div>
              <div className="news-meta">
                {hit.domain} · {fmtWhen(hit)}
                {hit.data_kind ? ` · ${hit.data_kind}` : ""}
              </div>
              {hit.snippet && <p className="news-snippet">{hit.snippet}</p>}
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
