"use client";

/**
 * News tab (docs/16-web-ui-news-tts.md): a Vietnamese news feed across
 * economics / finance / business / technology, backed by /v1/search with the
 * news engines (google_news) and freshness windows, plus Vietnamese TTS
 * (Web Speech API — lib/tts.ts) so the feed can be *listened* to, not only read.
 *
 * Freshness: the client sends only the structured `freshnessDays` param; the
 * backend maps it to Google News' `when:{n}d` operator server-side (docs/16),
 * so the UI only ever claims a window the engine actually enforces. If a
 * custom query embeds its own `when:` operator the backend respects it and
 * the toolbar says so instead of claiming the preset window.
 *
 * Data integrity: every item renders source domain + published time straight
 * from the backend payload (published_at/as_of, never guessed client-side);
 * `complete === false` renders an explicit partial-results notice.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import AnswerMarkdown from "./AnswerMarkdown";

import {
  addWatchlist,
  createPersonalPodcast,
  createPodcastBriefing,
  fetchMarketEvents,
  fetchMarketNewsWire,
  generateBriefing,
  latestBriefing,
  listWatchlist,
  personalPodcastLatestUrl,
  personalPodcastStatus,
  podcastBriefingStatus,
  podcastFeedUrl,
  podcastLatestUrl,
  removeWatchlist,
  safeHref,
  SearchHit,
  search,
  ttsCapabilities,
  watchlistAlerts,
  type BriefingDetail,
  type BriefingSource,
  type Citation,
  type MarketEventsResponse,
  type MarketNewsWireResponse,
  type PodcastBriefingStatus,
  type WatchlistAlertsResponse,
  type WatchlistItem,
} from "../lib/api";
import {
  dictationIsSupported,
  mergeTranscripts,
  startDictation,
  type DictationHandle,
} from "../lib/dictation";
import {
  getPreferredEngine,
  setPreferredEngine,
  TtsEngine,
  TtsHandle,
  TtsVoiceMode,
  ttsHasVietnameseVoice,
  ttsIsSupported,
  ttsOnVoicesChanged,
  ttsSpeak,
  ttsSpeakRemote,
  ttsStop,
  ttsVietnameseVoiceMode,
} from "../lib/tts";

export type NewsCategory = {
  id: string;
  label: string;
  channelId: string;
  icon: string;
  query: string;
  engines: string[];
  freshnessDays: number;
};

export const NEWS_CATEGORIES: NewsCategory[] = [
  {
    id: "macro",
    channelId: "macro_economy",
    label: "Kinh tế vĩ mô",
    icon: "🏛️",
    query: "tin tức kinh tế vĩ mô Việt Nam GDP CPI",
    engines: ["google_news"],
    freshnessDays: 3,
  },
  {
    id: "finance",
    channelId: "markets_stocks",
    label: "Tài chính – Chứng khoán",
    icon: "📈",
    query: "thị trường chứng khoán Việt Nam VN-Index tin tức",
    engines: ["google_news"],
    freshnessDays: 2,
  },
  {
    id: "business",
    channelId: "business_corporate",
    label: "Kinh doanh – Doanh nghiệp",
    icon: "🏢",
    query: "tin kinh doanh doanh nghiệp Việt Nam",
    engines: ["google_news"],
    freshnessDays: 3,
  },
  {
    id: "tech",
    channelId: "tech_ai",
    label: "Công nghệ – AI",
    icon: "💻",
    query: "tin tức công nghệ AI phần mềm",
    engines: ["google_news"],
    freshnessDays: 3,
  },
  {
    id: "banking",
    channelId: "banking_fx",
    label: "Ngân hàng – Tiền tệ",
    icon: "🏦",
    query: "tin tức ngân hàng lãi suất chính sách tiền tệ",
    engines: ["google_news"],
    freshnessDays: 3,
  },
  {
    id: "world",
    channelId: "global_economy",
    label: "Kinh tế thế giới",
    icon: "🌍",
    query: "kinh tế thế giới thị trường toàn cầu tin tức",
    engines: ["google_news"],
    freshnessDays: 3,
  },
];

const NEWS_K = 15;
const READ_ALOUD_TOP_N = 8;
// Mirrors the backend guard (google_news.py _WHEN_OPERATOR_RE: end-anchored,
// ASCII-only — same semantics as Python re.ASCII): a real `when:` operator
// embedded in a custom query suppresses the server-side append, so the
// toolbar must not claim the preset N-day window.
const WHEN_OPERATOR_RE = /\bwhen:[0-9]+[dhwmy](?![a-z0-9])/i;

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
  const [voiceMode, setVoiceMode] = useState<TtsVoiceMode>("none");
  // Server-side TTS engine (docs/17): VoiceStudio only if the backend reports
  // it configured; otherwise the browser Web Speech API is used.
  const [engine, setEngine] = useState<TtsEngine>("webspeech");
  const [vsEnabled, setVsEnabled] = useState(false);
  const ttsHandleRef = useRef<TtsHandle | null>(null);
  // Request-id + abort guard: a slow in-flight response can never overwrite a
  // newer category's feed, and a hung backend cannot leave the tab disabled.
  const reqIdRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  // Dictation for the custom-query box (docs/18 A1).
  const [micReady, setMicReady] = useState(false);
  const [listening, setListening] = useState(false);
  const micHandleRef = useRef<DictationHandle | null>(null);
  const micBaseRef = useRef("");
  const [micHint, setMicHint] = useState("");
  // docs/33: expert AI briefing per category — generated on demand, rendered
  // inline above the raw headline list; also persisted to the Blog Chuyên Sâu feed.
  const [briefing, setBriefing] = useState<BriefingDetail | null>(null);
  const [briefingLoading, setBriefingLoading] = useState(false);
  const [briefingError, setBriefingError] = useState("");
  const [watchAddBusy, setWatchAddBusy] = useState(false);
  // Watchlist + alerts (docs/18 C) — poll-on-demand, no background poller.
  const [watchItems, setWatchItems] = useState<WatchlistItem[]>([]);
  const [alerts, setAlerts] = useState<WatchlistAlertsResponse | null>(null);
  const [checkingAlerts, setCheckingAlerts] = useState(false);
  const [watchError, setWatchError] = useState("");
  // Podcast briefing (docs/18 D + v3.0 P2-5 personalized watchlist) — VoiceStudio-only.
  const [podcast, setPodcast] = useState<PodcastBriefingStatus | null>(null);
  const [podcastMode, setPodcastMode] = useState<"global" | "personal">("global");
  const [podcastBusy, setPodcastBusy] = useState(false);
  const [podcastError, setPodcastError] = useState("");
  const podcastPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const podcastReqIdRef = useRef(0);
  const podcastAbortRef = useRef<AbortController | null>(null);
  // Market news wire & corporate events inspector (POST /v1/market/news-wire + /events).
  const [eventSymbolsInput, setEventSymbolsInput] = useState("");
  const [eventsLoading, setEventsLoading] = useState(false);
  const [eventsError, setEventsError] = useState("");
  const [eventsData, setEventsData] = useState<MarketEventsResponse | null>(null);
  const [wireData, setWireData] = useState<MarketNewsWireResponse | null>(null);
  const eventsReqIdRef = useRef(0);
  const eventsAbortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    // Voices/SpeechSynthesis exist only client-side — gate after mount to
    // avoid SSR/hydration mismatch.
    setTtsReady(ttsIsSupported());
    setHasViVoice(ttsHasVietnameseVoice());
    setMicReady(dictationIsSupported());
    setVoiceMode(ttsVietnameseVoiceMode());
    setEngine(getPreferredEngine());
    ttsCapabilities()
      .then((c) => setVsEnabled(Boolean(c.voicestudio.enabled)))
      .catch(() => setVsEnabled(false));
    // The voice list populates asynchronously (Chrome/Safari): re-check on
    // every voiceschanged so the vi-voice hint cannot stick at the wrong value.
    const offVoices = ttsOnVoicesChanged(() => {
      setHasViVoice(ttsHasVietnameseVoice());
      setVoiceMode(ttsVietnameseVoiceMode());
    });
    // Watchlist load is best-effort: a dead backend must not brick the tab.
    listWatchlist()
      .then((r) => {
        setWatchItems(r.items);
        if (r.storage === "memory") {
          setWatchError("Backend đang mất kết nối DB — watchlist tạm thời lưu trong bộ nhớ của server.");
        }
      })
      .catch(() => setWatchItems([]));
    podcastBriefingStatus()
      .then(setPodcast)
      .catch(() => setPodcast(null));
    return () => {
      offVoices();
      ttsStop();
      ttsHandleRef.current?.cancel();
      abortRef.current?.abort();
      podcastAbortRef.current?.abort();
      eventsAbortRef.current?.abort();
      micHandleRef.current?.stop();
      if (podcastPollRef.current) clearInterval(podcastPollRef.current);
    };
  }, []);

  const loadCorporateIntelligence = useCallback(async () => {
    const tokens = eventSymbolsInput
      .split(/[\s,;]+/)
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean)
      .slice(0, 10);
    eventsAbortRef.current?.abort();
    const ctrl = new AbortController();
    eventsAbortRef.current = ctrl;
    const reqId = ++eventsReqIdRef.current;
    setEventsLoading(true);
    setEventsError("");
    setEventsData(null);
    setWireData(null);
    try {
      const [evRes, wireRes] = await Promise.allSettled([
        fetchMarketEvents({ symbols: tokens, lookahead_days: 60 }, ctrl.signal),
        fetchMarketNewsWire({ symbols: tokens, max_items: 15 }, ctrl.signal),
      ]);
      if (ctrl.signal.aborted || reqId !== eventsReqIdRef.current) return;
      const errs: string[] = [];
      if (evRes.status === "fulfilled") {
        setEventsData(evRes.value);
      } else {
        errs.push(`Sự kiện: ${evRes.reason instanceof Error ? evRes.reason.message : String(evRes.reason)}`);
      }
      if (wireRes.status === "fulfilled") {
        setWireData(wireRes.value);
      } else {
        errs.push(`Dòng tin: ${wireRes.reason instanceof Error ? wireRes.reason.message : String(wireRes.reason)}`);
      }
      if (errs.length > 0) {
        setEventsError(errs.join(" · "));
      }
    } finally {
      if (!ctrl.signal.aborted && reqId === eventsReqIdRef.current) {
        setEventsLoading(false);
      }
    }
  }, [eventSymbolsInput]);

  const toggleMic = () => {
    if (listening) {
      micHandleRef.current?.stop();
      micHandleRef.current = null;
      setListening(false);
      return;
    }
    micBaseRef.current = customQuery;
    setMicHint("");
    const handle = startDictation({
      onUpdate: (committed, interim) => {
        setCustomQuery(mergeTranscripts(micBaseRef.current, committed, interim));
      },
      onEnd: (reason) => {
        setListening(false);
        setMicHint(
          reason === "permission"
            ? "❌ Trình duyệt chặn micro — cấp quyền rồi thử lại."
            : reason === "no-microphone"
              ? "❌ Không tìm thấy micro trên máy."
              : reason === "network"
                ? "❌ Nhận dạng giọng nói lỗi mạng — thử lại."
                : reason === "no-speech"
                  ? "⚠️ Không nghe thấy lời nói — nói lại sau khi bấm 🎙️."
                  : ""
        );
      },
    });
    if (handle) {
      micHandleRef.current = handle;
      setListening(true);
    }
  };

  const activeCat = NEWS_CATEGORIES.find((c) => c.id === activeCatId) ?? NEWS_CATEGORIES[0];

  // --- watchlist handlers (docs/18 C) ---------------------------------------
  const refreshWatchlist = useCallback(async () => {
    try {
      const r = await listWatchlist();
      setWatchItems(r.items);
      setWatchError(r.storage === "memory" ? "Watchlist đang lưu tạm trong bộ nhớ (DB lỗi)." : "");
    } catch (e) {
      setWatchError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const addCurrentToWatchlist = useCallback(async () => {
    const q = (customQuery.trim() || appliedQuery || "").trim();
    if (!q || watchAddBusy) return;
    setWatchAddBusy(true);
    try {
      await addWatchlist(q, activeCat.label);
      await refreshWatchlist();
      setWatchError("");
    } catch (e) {
      setWatchError(e instanceof Error ? e.message : String(e));
    } finally {
      setWatchAddBusy(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [customQuery, appliedQuery, activeCat.label, refreshWatchlist, watchAddBusy]);

  const deleteWatchItem = useCallback(
    async (id: string) => {
      try {
        await removeWatchlist(id);
        await refreshWatchlist();
      } catch (e) {
        setWatchError(e instanceof Error ? e.message : String(e));
      }
    },
    [refreshWatchlist]
  );

  const checkAlerts = useCallback(async () => {
    setCheckingAlerts(true);
    try {
      const resp = await watchlistAlerts();
      setAlerts(resp);
      setWatchError("");
    } catch (e) {
      setWatchError(e instanceof Error ? e.message : String(e));
    } finally {
      setCheckingAlerts(false);
    }
  }, []);

  // Plain function (not useCallback): it closes over speakText/stopSpeaking,
  // which are re-created each render and must never go stale here.
  const speakNewItems = () => {
    if (!alerts) return;
    if (speakingId === "__alerts__") {
      stopSpeaking();
      return;
    }
    const lines: string[] = [];
    for (const item of alerts.items) {
      for (const n of item.new_items) lines.push(n.title || n.url);
    }
    if (!lines.length) return;
    stopSpeaking();
    ttsHandleRef.current = speakText(`Có ${alerts.total_new} tin mới. ${lines.join(". ")}.`, {
      onStart: () => setSpeakingId("__alerts__"),
      onEnd: () => setSpeakingId(null),
      onError: () => setSpeakingId(null),
    });
  };

  // --- podcast handlers (docs/18 D + v3.0 personal watchlist) ---------------
  const startPodcast = useCallback(async (kind: "global" | "personal" = "global") => {
    const reqId = ++podcastReqIdRef.current;
    podcastAbortRef.current?.abort();
    const ctrl = new AbortController();
    podcastAbortRef.current = ctrl;
    if (podcastPollRef.current) {
      clearInterval(podcastPollRef.current);
      podcastPollRef.current = null;
    }
    setPodcastBusy(true);
    setPodcastMode(kind);
    setPodcast(null);
    setPodcastError("");
    try {
      const createFn = kind === "personal" ? createPersonalPodcast : createPodcastBriefing;
      const statusFn = kind === "personal" ? personalPodcastStatus : podcastBriefingStatus;
      const st = await createFn(ctrl.signal);
      if (ctrl.signal.aborted || podcastReqIdRef.current !== reqId) return;
      setPodcast(st);
      if (st.status === "ready") return;
      let consecutiveErrors = 0;
      let pollAttempts = 0;
      podcastPollRef.current = setInterval(async () => {
        if (ctrl.signal.aborted || podcastReqIdRef.current !== reqId) {
          if (podcastPollRef.current) clearInterval(podcastPollRef.current);
          podcastPollRef.current = null;
          return;
        }
        pollAttempts += 1;
        if (pollAttempts > 150) {
          if (podcastPollRef.current) clearInterval(podcastPollRef.current);
          podcastPollRef.current = null;
          setPodcastError("Hết thời gian chờ bản tin audio — thử tạo lại.");
          return;
        }
        try {
          consecutiveErrors = 0;
          const s = await statusFn(ctrl.signal);
          if (ctrl.signal.aborted || podcastReqIdRef.current !== reqId) return;
          setPodcast(s);
          if (s.status === "ready" || s.status === "error" || s.status === "cancelled") {
            if (podcastPollRef.current) clearInterval(podcastPollRef.current);
            podcastPollRef.current = null;
            if (s.status === "cancelled") {
              setPodcastError("Tạo bản tin audio bị huỷ (server tắt) — thử lại.");
            }
          }
        } catch {
          if (ctrl.signal.aborted || podcastReqIdRef.current !== reqId) return;
          consecutiveErrors += 1;
          if (consecutiveErrors >= 5) {
            if (podcastPollRef.current) clearInterval(podcastPollRef.current);
            podcastPollRef.current = null;
            setPodcastError("Mất kết nối backend khi chờ bản tin — thử lại sau.");
          }
        }
      }, 2000);
    } catch (e) {
      if (ctrl.signal.aborted || podcastReqIdRef.current !== reqId) return;
      setPodcastError(e instanceof Error ? e.message : String(e));
    } finally {
      if (!ctrl.signal.aborted && podcastReqIdRef.current === reqId) setPodcastBusy(false);
    }
  }, []);

  // VoiceStudio configured on the backend wins over the browser engine.
  const effectiveEngine: TtsEngine =
    engine === "voicestudio" && !vsEnabled ? "webspeech" : engine;

  // Privacy wording that matches the engine/voice actually in use — never a
  // generic claim when the picked voice is a cloud (online) one.
  const privacyNote =
    effectiveEngine === "voicestudio"
      ? "Riêng tư: nội dung đọc được gửi tới máy chủ VoiceStudio nội bộ (cấu hình VOICESTUDIO_URL ở backend), không ra Internet."
      : voiceMode === "local"
        ? "Riêng tư: đang dùng giọng nội bộ của hệ điều hành — nội dung đọc không rời khỏi máy."
        : voiceMode === "online"
          ? "Riêng tư: đang dùng giọng online (Edge/Microsoft) — nội dung đọc được gửi từ trình duyệt tới máy chủ TTS."
          : "Trình duyệt sẽ đọc bằng giọng mặc định vì chưa có giọng tiếng Việt trên máy.";

  const speakText = (
    text: string,
    cbs: { onStart?: () => void; onEnd?: () => void; onError?: () => void },
  ): TtsHandle | null =>
    effectiveEngine === "voicestudio"
      ? ttsSpeakRemote(text, { ...cbs, fallbackToWebSpeech: true })
      : ttsSpeak(text, cbs);

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
      // The client sends only the structured params (freshnessDays + news);
      // the backend maps freshness_days → `when:{n}d` for google_news
      // server-side (docs/16 §1) — the UI never embeds engine operators.
      const q = (overrideQuery ?? cat.query).trim() || cat.query;
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

  const openBriefing = useCallback(
    async (mode: "generate" | "latest") => {
      const cat = NEWS_CATEGORIES.find((c) => c.id === activeCatId) ?? NEWS_CATEGORIES[0];
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 120_000);
      setBriefingLoading(true);
      setBriefingError("");
      try {
        const detail =
          mode === "generate"
            ? await generateBriefing(cat.channelId, controller.signal)
            : await latestBriefing(cat.channelId, controller.signal);
        setBriefing(detail);
      } catch (e) {
        setBriefingError(
          e instanceof Error ? e.message : String(e)
        );
      } finally {
        clearTimeout(timer);
        setBriefingLoading(false);
      }
    },
    [activeCatId]
  );

  const briefingCitationMap = new Map<number, BriefingSource>();
  (briefing?.sources ?? []).forEach((s) => briefingCitationMap.set(s.n, s));

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
    ttsHandleRef.current = speakText(text, {
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
    ttsHandleRef.current = speakText(text, {
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
        {micHint && <span className="news-tts-hint">{micHint}</span>}
        {micReady && (
          <button
            type="button"
            className={`tts-btn ${listening ? "speaking" : ""}`}
            onClick={toggleMic}
            aria-pressed={listening}
            aria-label={listening ? "Dừng ghi âm giọng nói" : "Nói truy vấn bằng giọng nói"}
            title={listening ? "Đang nghe... bấm để dừng" : "Nói truy vấn bằng giọng nói (tiếng Việt). Lưu ý riêng tư: Chrome gửi audio lên máy chủ nhận diện giọng nói để chuyển thành chữ."}
          >
            {listening ? "⏹" : "🎙️"}
          </button>
        )}
        <button type="submit" disabled={loading || !customQuery.trim()}>
          {loading ? "Đang tải..." : "Lọc tin"}
        </button>
      </form>

      {/* docs/33: expert AI briefing — evidence-windowed persona synthesis with
          deterministic grounding verification + council review (Step A/B). */}
      <div className="news-toolbar">
        <button
          type="button"
          className="tts-btn"
          onClick={() => void openBriefing("generate")}
          disabled={briefingLoading}
          title={`Tổng hợp bản tin chuyên sâu AI cho ${activeCat.label} (persona + cửa sổ thời gian + phản biện council; cần quyền admin).`}
        >
          {briefingLoading ? "Đang tổng hợp AI..." : `🧠 Tổng hợp chuyên gia (${activeCat.label})`}
        </button>
        <button
          type="button"
          className="tts-btn"
          onClick={() => void openBriefing("latest")}
          disabled={briefingLoading}
          title="Xem bản tin chuyên sâu mới nhất đã lưu cho mục này."
        >
          📄 Bản tin đã lưu
        </button>
      </div>
      {briefingError && <p className="news-info briefing-error">{briefingError}</p>}
      {briefing && !briefingError && (
        <section className="briefing-block">
          <h3 style={{ marginTop: 0 }}>{briefing.title}</h3>
          {briefing.summary && <p className="briefing-summary">{briefing.summary}</p>}
          <AnswerMarkdown
            text={briefing.content_md}
            citationByUrl={briefingCitationMap as unknown as Map<number, Citation>}
          />
        </section>
      )}

      <div className="news-toolbar">
        <button
          type="button"
          className="tts-btn"
          onClick={() => void startPodcast("global")}
          disabled={podcastBusy}
          title={
            vsEnabled
              ? "Tạo bản tin audio trong ngày bằng VoiceStudio (server-side) và phát trong tab này; RSS feed để nghe bằng app podcast."
              : "Cần cấu hình VOICESTUDIO_URL ở backend (docs/17) — hiện tại đang dùng đọc bằng trình duyệt."
          }
        >
          {podcastBusy && podcastMode === "global" ? "Đang tạo bản tin..." : "🎙️ Bản tin audio"}
        </button>
        <button
          type="button"
          className="tts-btn"
          onClick={() => void startPodcast("personal")}
          disabled={podcastBusy}
          title="Tạo podcast cá nhân hoá dựa trên danh sách Watchlist (cần PODCAST_PERSONAL_ENABLED=true và VOICESTUDIO_URL)."
        >
          {podcastBusy && podcastMode === "personal" ? "Đang tạo..." : "🎧 Podcast Watchlist"}
        </button>
        <label className="news-tts-engine">
          Giọng đọc:{" "}
          <select
            aria-label="Chọn engine đọc máy"
            value={effectiveEngine}
            onChange={(e) => {
              const v = e.target.value as TtsEngine;
              setEngine(v);
              setPreferredEngine(v);
            }}
          >
            <option value="webspeech">Trình duyệt (Web Speech)</option>
            <option value="voicestudio" disabled={!vsEnabled}>
              VoiceStudio (server{vsEnabled ? "" : " — chưa cấu hình"})
            </option>
          </select>
        </label>
        <span className="news-info">
          {appliedQuery ? (
            <>
              Truy vấn: <code>{appliedQuery}</code> ·{" "}
              {WHEN_OPERATOR_RE.test(appliedQuery)
                ? "cửa sổ: theo when: trong truy vấn"
                : `cửa sổ ${activeCat.freshnessDays} ngày`}{" "}
              · nguồn: {activeCat.engines.join(", ")}
              {cached ? " · bản đã cache" : ""}
            </>
          ) : (
            "Chọn một mục hoặc nhập truy vấn riêng."
          )}
        </span>
        {(ttsReady || vsEnabled) && hits.length > 0 && (
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
              title={`Đọc ${Math.min(READ_ALOUD_TOP_N, hits.length)} tin mới nhất bằng giọng đọc tiếng Việt. ${privacyNote}`}
            >
              🔊 Đọc tin mới nhất
            </button>
          )
        )}
        {!ttsReady && !vsEnabled && (
          <span className="news-tts-hint">
            🔇 Trình duyệt không hỗ trợ đọc máy (Web Speech API) và backend chưa cấu hình
            VoiceStudio — dùng Chrome/Edge hoặc đặt VOICESTUDIO_URL để nghe tin.
          </span>
        )}
        {ttsReady && !hasViVoice && (
          <span className="news-tts-hint">
            ⚠️ Chưa có giọng tiếng Việt trên máy — tiếng Việt sẽ đọc bằng giọng mặc định.
            Cài giọng vi-VN để đọc chuẩn; lưu ý giọng online (Edge) sẽ gửi nội dung đọc từ trình
            duyệt tới máy chủ TTS — chọn giọng nội bộ (local) nếu cần riêng tư.
          </span>
        )}
        {ttsReady && voiceMode === "online" && (
          <span className="news-tts-hint">
            🌐 Đang dùng giọng online — nội dung đọc được gửi từ trình duyệt tới máy chủ TTS.
            Chọn một giọng nội bộ (local) trong cài đặt giọng đọc của trình duyệt để giữ riêng
            tư trên máy.
          </span>
        )}
        {ttsReady && voiceMode === "local" && (
          <span className="news-tts-hint">🔒 Giọng đọc nội bộ — nội dung không rời khỏi máy.</span>
        )}
      </div>

      {podcastError && <p className="error">Bản tin audio: {podcastError}</p>}
      {podcast && podcast.status === "ready" && (
        <div className="podcast-player">
          <audio
            key={`${podcastMode}-${podcast.date}-${podcast.audio_bytes}`}
            controls
            preload="none"
            src={podcastMode === "personal" ? personalPodcastLatestUrl() : podcastLatestUrl()}
            style={{ width: "100%" }}
            onError={() => setPodcastError("Không tải được audio (401 nếu AUTH_ENABLED=true — trình duyệt không gửi API key qua <audio>; hoặc file chưa sẵn sàng).")}
          >
            Trình duyệt không hỗ trợ audio.
          </audio>
          <span className="muted small">
            {podcastMode === "personal" ? "Podcast Watchlist" : "Bản tin"} ngày {podcast.date} ·{" "}
            {podcast.audio_bytes} byte
            {podcastMode === "global" && (
              <>
                {" "}
                · <a href={podcastFeedUrl()} target="_blank" rel="noreferrer">RSS feed</a>
              </>
            )}{" "}
            <span className="muted small">(RSS/audio cần API key qua header khi bật auth — dùng client MCP/REST)</span>
          </span>
        </div>
      )}
      {podcast && (podcast.status === "pending" || podcast.status === "running") && (
        <p className="news-loading">Đang tổng hợp và đọc bản tin ngày {podcast.date}...</p>
      )}
      {podcast && podcast.status === "error" && (
        <p className="error">Bản tin lỗi: {podcast.error ?? "không rõ"}</p>
      )}

      <div className="watchlist-block">
        <div className="watchlist-head">
          <strong>Theo dõi truy vấn</strong>
          <button
            type="button"
            className="tts-btn"
            onClick={addCurrentToWatchlist}
            disabled={watchAddBusy}
            aria-busy={watchAddBusy}
          >
            {watchAddBusy ? "Đang thêm..." : "+ Theo dõi truy vấn này"}
          </button>
          <button
            type="button"
            className="tts-btn"
            onClick={checkAlerts}
            disabled={checkingAlerts || watchItems.length === 0}
          >
            {checkingAlerts ? "Đang kiểm tra..." : "Kiểm tra tin mới"}
          </button>
          {alerts && alerts.total_new > 0 && (
            <>
              <span className="chip chip-toggle on">Mới: {alerts.total_new}</span>
              {(ttsReady || vsEnabled) && (
                <button
                  type="button"
                  className={`tts-btn ${speakingId === "__alerts__" ? "speaking" : ""}`}
                  onClick={speakNewItems}
                  aria-label="Đọc các tin mới"
                  title="Đọc các tin mới bằng giọng tiếng Việt"
                >
                  {speakingId === "__alerts__" ? "⏹" : "🔊"}
                </button>
              )}
            </>
          )}
        </div>
        {watchError && <p className="error">{watchError}</p>}
        {watchItems.length > 0 && (
          <ul className="watchlist-items">
            {watchItems.map((w) => (
              <li key={w.id}>
                <code>{w.query}</code>
                {w.label && <span className="muted"> · {w.label}</span>}
                <button
                  type="button"
                  className="tts-btn"
                  onClick={() => void deleteWatchItem(w.id)}
                  aria-label={`Xoá theo dõi ${w.query}`}
                >
                  ✕
                </button>
              </li>
            ))}
          </ul>
        )}
        {alerts && alerts.items.length > 0 && (
          <div className="watchlist-alerts">
            {alerts.items.map((a) => (
              <div key={a.id} className="watchlist-alert">
                <strong>{a.query}</strong>
                {a.error ? (
                  <span className="error"> lỗi: {a.error}</span>
                ) : a.baseline_set ? (
                  <span className="muted"> · đã lưu mốc nền (lần kiểm tra đầu)</span>
                ) : a.new_items.length === 0 ? (
                  <span className="muted"> · không có tin mới</span>
                ) : (
                  <ul>
                    {a.new_items.map((n) => (
                      <li key={n.url}>
                        <a href={safeHref(n.url)} target="_blank" rel="noreferrer">
                          {n.title || n.url}
                        </a>{" "}
                        <span className="muted small">
                          {n.domain}
                          {n.published_at ? ` · ${n.published_at.slice(0, 10)}` : ""}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="watchlist-block">
        <form
          className="watchlist-head"
          onSubmit={(e) => {
            e.preventDefault();
            if (!eventsLoading) void loadCorporateIntelligence();
          }}
        >
          <strong>Sự kiện doanh nghiệp &amp; Dòng tin cấu trúc</strong>
          <input
            type="text"
            className="news-query-input"
            value={eventSymbolsInput}
            onChange={(e) => setEventSymbolsInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && e.nativeEvent.isComposing) {
                e.preventDefault();
              }
            }}
            disabled={eventsLoading}
            placeholder="Mã CP (VD: FPT, VNM, HPG) — để trống xem toàn thị trường"
            aria-label="Mã cổ phiếu tra cứu sự kiện và dòng tin"
            style={{ maxWidth: "24rem" }}
          />
          <button
            type="submit"
            className="tts-btn"
            disabled={eventsLoading}
            aria-busy={eventsLoading}
          >
            {eventsLoading ? "Đang tải..." : "📅 Tra sự kiện & Dòng tin"}
          </button>
        </form>
        {eventsError && <p className="error">Lỗi tra cứu sự kiện: {eventsError}</p>}
        {(eventsData || wireData) && (
          <div style={{ marginTop: "0.5rem" }}>
            <p className="muted small">
              {eventsData && (
                <>
                  Sự kiện: <strong>{eventsData.events.length}</strong> · Lịch GDKHQ sắp tới:{" "}
                  <strong>{eventsData.upcoming_ex_dates.length}</strong> · Pha loãng:{" "}
                  <strong>{eventsData.dilution_alerts.length}</strong> · Rủi ro:{" "}
                  <strong>{eventsData.risk_alerts.length}</strong>
                </>
              )}
              {wireData && (
                <>
                  {eventsData ? " · " : ""}Dòng tin khử trùng:{" "}
                  <strong>
                    {wireData.items.length}/{wireData.deduplicated_count || wireData.total_raw}
                  </strong>
                </>
              )}
              {(eventsData?.partial || wireData?.partial) && " · ⚠️ Một phần nguồn tạm gián đoạn"}
            </p>
            {(eventsData?.warnings.length || wireData?.warnings.length) ? (
              <p className="muted small">
                Lưu ý: {[...(eventsData?.warnings ?? []), ...(wireData?.warnings ?? [])].slice(0, 3).join(" | ")}
              </p>
            ) : null}
            {eventsData && eventsData.upcoming_ex_dates.length > 0 && (
              <ul className="watchlist-list" style={{ marginTop: "0.35rem" }}>
                {eventsData.upcoming_ex_dates.slice(0, 4).map((ev) => {
                  const href = ev.source_url ? safeHref(ev.source_url) : undefined;
                  return (
                    <li key={`ex-${ev.event_id}`} className="watchlist-alert-item">
                      <span>
                        <strong>{ev.symbol ?? "MKT"}</strong> [Lịch GDKHQ {ev.ex_date}]{" "}
                        {href ? (
                          <a href={href} target="_blank" rel="noreferrer">
                            {ev.title}
                          </a>
                        ) : (
                          ev.title
                        )}{" "}
                        <span className="muted small">
                          {[
                            ev.cash_dividend_pct ? `cổ tức ${ev.cash_dividend_pct}%` : null,
                            ev.cash_per_share_vnd
                              ? `${ev.cash_per_share_vnd.toLocaleString()}đ/cp`
                              : null,
                            ev.stock_ratio ? `tỷ lệ ${ev.stock_ratio}` : null,
                          ]
                            .filter(Boolean)
                            .join(" · ")}
                        </span>
                      </span>
                    </li>
                  );
                })}
              </ul>
            )}
            {eventsData && eventsData.events.length > 0 && (
              <ul className="watchlist-list" style={{ marginTop: "0.35rem" }}>
                {eventsData.events.slice(0, 8).map((ev) => {
                  const href = ev.source_url ? safeHref(ev.source_url) : undefined;
                  return (
                    <li key={ev.event_id} className="watchlist-alert-item">
                      <span>
                        <strong>{ev.symbol ?? "MKT"}</strong> [{ev.event_type}/{ev.event_subtype}]{" "}
                        {href ? (
                          <a href={href} target="_blank" rel="noreferrer">
                            {ev.title}
                          </a>
                        ) : (
                          ev.title
                        )}{" "}
                        <span className="muted small">
                          {ev.ex_date ? `GDKHQ ${ev.ex_date}` : ev.event_date ?? "chưa rõ ngày"}
                          {ev.share_factor ? ` · hệ số ${ev.share_factor}x` : ""}
                          {ev.cash_dividend_pct ? ` · cổ tức ${ev.cash_dividend_pct}%` : ""}
                          {ev.cash_per_share_vnd ? ` · ${ev.cash_per_share_vnd.toLocaleString()}đ/cp` : ""}
                        </span>
                      </span>
                    </li>
                  );
                })}
              </ul>
            )}
            {wireData && wireData.items.length > 0 && (
              <ul className="watchlist-list" style={{ marginTop: "0.35rem" }}>
                {wireData.items.slice(0, 6).map((item, idx) => {
                  const href = item.url ? safeHref(item.url) : undefined;
                  return (
                    <li key={`${item.stable_id}-${idx}`} className="watchlist-alert-item">
                      <span>
                        <strong>{item.symbols[0] ?? item.scope.toUpperCase()}</strong> [
                        {item.category}]{" "}
                        {href ? (
                          <a href={href} target="_blank" rel="noreferrer">
                            {item.title}
                          </a>
                        ) : (
                          item.title
                        )}{" "}
                        <span className="muted small">
                          {item.source_id}
                          {item.duplicate_count > 1 ? ` (+${item.duplicate_count - 1} nguồn)` : ""}
                        </span>
                      </span>
                    </li>
                  );
                })}
              </ul>
            )}
            {!eventsError &&
              eventsData !== null &&
              wireData !== null &&
              eventsData.events.length === 0 &&
              wireData.items.length === 0 && (
              <p className="news-loading">
                Không tìm thấy sự kiện hay dòng tin phù hợp cho điều kiện vừa tra cứu.
              </p>
            )}
          </div>
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
                {(ttsReady || vsEnabled) && (
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
                      title={`Đọc tin này bằng giọng đọc tiếng Việt. ${privacyNote}`}
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
