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
import { buildNewsQuery } from "../lib/newsQuery";
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
  // A future/garbled source timestamp must not render as "vừa xong" — show
  // the claimed date instead of inventing a recency.
  if (diffMs < 0) return new Date(ts).toISOString().slice(0, 10);
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
  const [cached, setCached] = useState(false);
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
    // Superseded = a newer request started: drop the response silently.
    // Aborted = unmount or the 30s client-side timeout fired: must surface an
    // error and ALWAYS release the loading state, or the tab bricks.
    const superseded = () => reqIdRef.current !== myId;
    // A hung backend must not leave the tab disabled forever (server caps at
    // 60s; this client-side belt-and-braces aborts at 30s).
    const timer = setTimeout(() => controller.abort(), 30_000);

    setLoading(true);
    setError("");
    ttsHandleRef.current?.cancel();
    ttsStop();
    setSpeakingId(null);
    try {
      // google_news ignores freshness_days server-side; `when:Nd` is the
      // operator its RSS feed actually honors (docs/16 §1) — built by the
      // pure, unit-tested buildNewsQuery (never appends a second operator).
      const q = buildNewsQuery(overrideQuery ?? cat.query, cat.query, cat.freshnessDays, cat.engines);
      setAppliedQuery(q);
      const resp = await search(q, {
        engines: cat.engines,
        categories: ["news"],
        freshnessDays: cat.freshnessDays,
        news: true, // news cache TTL (900s) + news freshness weight on the backend
        k: NEWS_K,
        signal: controller.signal,
      });
      if (superseded()) return;
      setHits(resp.results ?? []);
      setComplete(resp.complete);
      setCached(Boolean(resp.cached));
    } catch (e) {
      if (superseded()) return;
      setHits([]);
      setComplete(undefined);
      setCached(false);
      setError(
        controller.signal.aborted
          ? "Hết thời gian chờ 30s khi tải tin — thử lại hoặc đổi mục."
          : e instanceof Error
            ? e.message
            : String(e)
      );
    } finally {
      clearTimeout(timer);
      if (!superseded()) setLoading(false);
    }
  }, []);

  // Load the default category when the tab opens (parent mounts this panel
  // only while its tab is active).
  useEffect(() => {
    void fetchNews(NEWS_CATEGORIES.find((c) => c.id === activeCatId) ?? NEWS_CATEGORIES[0]);
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
        // missing, drops the leading "[Nguồn]" tag google_news prefixes, and
        // strips trailing punctuation so no ".." is spoken.
        const stripEnd = (s: string) => s.replace(/[.!?…:;]+$/, "");
        const title = stripEnd(h.title || h.domain);
        const snippet = stripEnd((h.snippet ?? "").replace(/^\[[^\]]+\]\s*/, ""));
        const source = h.title ? ` Nguồn ${h.domain}.` : "";
        return `Tin ${i + 1}: ${title}. ${snippet ? `${snippet}.` : ""}${source}`;
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
    const stripEnd = (s: string) => s.replace(/[.!?…:;]+$/, "");
    const snippet = stripEnd((hit.snippet ?? "").replace(/^\[[^\]]+\]\s*/, ""));
    const title = stripEnd(hit.title || hit.domain);
    const source = hit.title ? ` Nguồn ${hit.domain}.` : "";
    const text = `${title}. ${snippet}${source}`;
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
              {cached ? " · bản đã cache" : ""}
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
