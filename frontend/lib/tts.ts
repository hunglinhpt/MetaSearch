/**
 * Vietnamese text-to-speech for the MetaSearch Web UI.
 *
 * Engine choice (docs/16-web-ui-news-tts.md): the browser's Web Speech API,
 * i.e. the OS voice stack — on Windows, Edge/Chrome expose vi-VN voices
 * (including Edge's online natural voices vi-VN-HoaiMy / vi-VN-NamMinh).
 * Free, zero backend dependency, zero server egress. Alternatives
 * (edge-tts python lib, server-side Windows SAPI) trade a new dependency and
 * audio plumbing for no user-visible gain — documented, not chosen.
 *
 * Graceful degradation: unsupported browsers get `ttsIsSupported() === false`
 * and callers hide the controls; a browser without a vi-* voice still speaks
 * with the default voice (the lang hint stays vi-VN).
 *
 * Privacy note: the speech NEVER leaves the MetaSearch backend, but with
 * Edge's *online* natural voices the utterance text is sent from the browser
 * to Microsoft's TTS service; local OS voices keep it on-device.
 */

import { ttsSynthesize } from "./api";

// Generation counter: any new speak/stop invalidates every chain from older
// generations, so an engine that fires `onend` after an external
// `synth.cancel()` (Firefox/Safari) cannot resume a stale chain behind a new
// speaker.
let _generation = 0;

// --- engine selection (docs/17) -------------------------------------------

export type TtsEngine = "webspeech" | "voicestudio";

const ENGINE_STORAGE_KEY = "metasearch.tts-engine";

/** Preferred TTS engine; "webspeech" unless the user picked VoiceStudio. */
export function getPreferredEngine(): TtsEngine {
  if (typeof window === "undefined") return "webspeech";
  try {
    return window.localStorage.getItem(ENGINE_STORAGE_KEY) === "voicestudio"
      ? "voicestudio"
      : "webspeech";
  } catch {
    return "webspeech"; // private mode etc.
  }
}

export function setPreferredEngine(engine: TtsEngine): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(ENGINE_STORAGE_KEY, engine);
  } catch {
    // storage unavailable — preference simply will not persist
  }
}

let _voices: SpeechSynthesisVoice[] = [];
let _voicesLoaded = false;

function loadVoices(): void {
  if (typeof window === "undefined" || !("speechSynthesis" in window)) return;
  _voices = window.speechSynthesis.getVoices();
  _voicesLoaded = _voices.length > 0;
}

if (typeof window !== "undefined" && "speechSynthesis" in window) {
  loadVoices();
  // Voices populate asynchronously in Chrome; the event re-fires loadVoices.
  window.speechSynthesis.addEventListener("voiceschanged", loadVoices);
}

export function ttsIsSupported(): boolean {
  return typeof window !== "undefined" && "speechSynthesis" in window;
}

export function ttsHasVietnameseVoice(): boolean {
  if (!ttsIsSupported()) return false;
  if (!_voicesLoaded) loadVoices();
  return _voices.some((v) => v.lang.toLowerCase().startsWith("vi"));
}

export type TtsVoiceMode = "local" | "online" | "none";

/**
 * Privacy-relevant mode of the vi-VN voice that `ttsSpeak` would actually
 * pick (pickVoice prefers on-device voices): "local" = OS voice, the spoken
 * text never leaves the machine; "online" = cloud voice (e.g. Edge neural),
 * the utterance text is sent from the browser to the vendor's TTS service;
 * "none" = no vi-* voice installed.
 */
export function ttsVietnameseVoiceMode(): TtsVoiceMode {
  if (!ttsHasVietnameseVoice()) return "none";
  const voice = pickVoice("vi-VN");
  if (!voice) return "none";
  return voice.localService ? "local" : "online";
}

/**
 * Subscribe to async voice-list changes (Chrome/Safari populate getVoices()
 * only after the first load); returns an unsubscribe function. Use this to
 * re-evaluate `ttsHasVietnameseVoice()` so a mount-time check cannot stick
 * at the wrong value forever.
 */
export function ttsOnVoicesChanged(cb: () => void): () => void {
  if (!ttsIsSupported()) return () => {};
  const handler = () => cb();
  window.speechSynthesis.addEventListener("voiceschanged", handler);
  return () => window.speechSynthesis.removeEventListener("voiceschanged", handler);
}

function pickVoice(lang: string): SpeechSynthesisVoice | null {
  if (!_voicesLoaded) loadVoices();
  const wanted = lang.toLowerCase();
  const base = wanted.split("-")[0];
  // 1. exact lang match (vi-VN), 2. any voice of the same language family
  // (vi-*), 3. null → browser default voice with the lang hint. Within a tier
  // prefer on-device voices (localService) over online neural ones, so spoken
  // content stays on the machine unless no local voice exists.
  const byLocalFirst = (a: SpeechSynthesisVoice, b: SpeechSynthesisVoice) =>
    Number(b.localService) - Number(a.localService);
  const exact = _voices
    .filter((v) => v.lang.toLowerCase() === wanted)
    .sort(byLocalFirst);
  if (exact.length > 0) return exact[0];
  const family = _voices
    .filter((v) => v.lang.toLowerCase().startsWith(base))
    .sort(byLocalFirst);
  return family[0] ?? null;
}

/** Strip markdown and citation markers so the voice reads prose, not syntax. */
export function stripForSpeech(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, " ") // fenced code blocks
    // images and links tolerate one level of nested parens in the URL
    .replace(/!\[[^\]]*\]\((?:[^()]|\([^()]*\))*\)/g, " ") // images
    .replace(/\[([^\]]*)\]\((?:[^()]|\([^()]*\))*\)/g, "$1") // links: keep the label only — a voice cannot use the URL
    .replace(/\[(\d+)\]/g, " ") // citation markers [12]
    .replace(/https?:\/\/\S+/g, " ") // bare URLs are unreadable noise when spoken aloud
    .replace(/`([^`]*)`/g, "$1") // inline code, keep content
    .replace(/&amp;/g, "&") // common entities
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, " ")
    .replace(/[\u200B\u200C\u200D\u2060]/g, "") // zero-width chars are not matched by \s
    .replace(/^#{1,6}\s+/gm, " ") // headings
    .replace(/^[\s\-|:]+$/gm, " ") // table separator rows (|---|---|) — hyphens would be spoken
    .replace(/[*_>|~]+/g, " ") // emphasis / table pipes / strikethrough
    .replace(/^\s*(?:[-+]|\d+[.)])\s+/gm, " ") // list bullets, incl. numbered "1. "
    .replace(/\s{2,}/g, " ")
    .trim();
}

/** Pack sentences under maxLen (speechSynthesis chokes on huge utterances). */
export function chunkForSpeech(text: string, maxLen = 220): string[] {
  maxLen = Math.max(1, Math.floor(maxLen)); // a non-positive maxLen would loop forever
  const sentences = text
    .split(/(?<=[.!?;:])\s+|\n+/)
    .map((s) => s.trim())
    .filter(Boolean);
  const chunks: string[] = [];
  let cur = "";
  const pushLong = (s: string) => {
    // A single sentence longer than maxLen: hard-split at word boundaries.
    while (s.length > maxLen) {
      const cut = s.lastIndexOf(" ", maxLen);
      const at = cut > maxLen * 0.5 ? cut : maxLen;
      chunks.push(s.slice(0, at).trim());
      s = s.slice(at).trim();
    }
    return s;
  };
  for (const s of sentences) {
    let sentence = pushLong(s);
    if (!sentence) continue;
    if (cur && (cur + " " + sentence).length > maxLen) {
      chunks.push(cur);
      cur = sentence;
    } else {
      cur = cur ? `${cur} ${sentence}` : sentence;
    }
  }
  if (cur) chunks.push(pushLong(cur));
  return chunks.filter(Boolean);
}

export type TtsOptions = {
  lang?: string;
  rate?: number;
  onStart?: () => void;
  onEnd?: () => void;
  onError?: (reason: string) => void;
  /** Remote chains only: speak the remaining text with Web Speech on failure
   * (docs/17 §2). Ignored by the Web Speech path itself. */
  fallbackToWebSpeech?: boolean;
};

export type TtsHandle = { cancel: () => void };

export function ttsSpeak(text: string, opts: TtsOptions = {}): TtsHandle | null {
  if (!ttsIsSupported()) {
    opts.onError?.("unsupported");
    return null;
  }
  const synth = window.speechSynthesis;
  synth.cancel(); // one speaker at a time — replaces anything in flight
  _activeRemote?.cancel(); // also stop a remote <audio> chain, if any
  _activeRemote = null;
  const gen = ++_generation; // invalidates chains of older speakers
  const clean = stripForSpeech(text);
  if (!clean) {
    opts.onEnd?.();
    return null;
  }
  const lang = opts.lang ?? "vi-VN";
  const voice = pickVoice(lang);
  const chunks = chunkForSpeech(clean);
  let idx = 0;
  let cancelled = false;
  const speakNext = () => {
    if (cancelled || gen !== _generation) return; // superseded by a new speaker
    if (idx >= chunks.length) {
      opts.onEnd?.();
      return;
    }
    try {
      const utterance = new SpeechSynthesisUtterance(chunks[idx++]);
      utterance.lang = lang;
      if (voice) utterance.voice = voice;
      utterance.rate = opts.rate ?? 1;
      utterance.onend = () => speakNext();
      utterance.onerror = (ev) => {
        if (cancelled || gen !== _generation) return; // our own cancel/replace
        const reason =
          typeof ev === "object" && ev !== null && "error" in ev
            ? String((ev as SpeechSynthesisErrorEvent).error)
            : String(ev);
        // "interrupted"/"canceled" are our own cancel() results, not faults.
        if (reason !== "canceled" && reason !== "interrupted") opts.onError?.(reason);
      };
      synth.speak(utterance);
    } catch {
      // some engines throw synchronously from speak() — never leave the
      // caller's speaking state stuck on an uncaught exception
      if (!cancelled && gen === _generation) opts.onError?.("speak-throw");
    }
  };
  opts.onStart?.();
  speakNext();
  return {
    cancel: () => {
      cancelled = true;
      _generation++; // any later speak gets a fresh generation anyway
      synth.cancel();
    },
  };
}

export function ttsStop(): void {
  // Stop a remote (VoiceStudio) playback too — it uses <audio>, not
  // speechSynthesis, so cancelling only the latter would let it run on.
  _activeRemote?.cancel();
  _activeRemote = null;
  if (ttsIsSupported()) {
    _generation++;
    window.speechSynthesis.cancel();
  }
}

// --- server-side (VoiceStudio) remote speak (docs/17) ----------------------

// In-session blob cache keyed by chunk text: re-reading the same news item
// must not re-synthesize. Bounded by entries AND bytes (a blob can be MBs).
// Server ETags are intentionally NOT stored client-side: the browser never
// re-sends If-None-Match on a POST (the server LRU + this in-session blob
// cache already cover reuse), so a stored etag would be dead weight (audit F5).
type RemoteCacheEntry = { blob: Blob };
const _remoteBlobCache = new Map<string, RemoteCacheEntry>();
const REMOTE_CACHE_MAX_ENTRIES = 24;
const REMOTE_CACHE_MAX_BYTES = 32 * 1024 * 1024;
let _remoteCacheBytes = 0;

function cacheGet(text: string): RemoteCacheEntry | undefined {
  const hit = _remoteBlobCache.get(text);
  if (hit) _remoteBlobCache.delete(text); // refresh recency
  if (hit) _remoteBlobCache.set(text, hit);
  return hit;
}

function cachePut(text: string, entry: RemoteCacheEntry): void {
  const old = _remoteBlobCache.get(text);
  if (old) _remoteCacheBytes -= old.blob.size;
  _remoteBlobCache.set(text, entry);
  _remoteCacheBytes += entry.blob.size;
  while (
    _remoteBlobCache.size > REMOTE_CACHE_MAX_ENTRIES ||
    _remoteCacheBytes > REMOTE_CACHE_MAX_BYTES
  ) {
    const oldestKey = _remoteBlobCache.keys().next().value;
    if (oldestKey === undefined) break;
    const oldest = _remoteBlobCache.get(oldestKey);
    if (oldest) _remoteCacheBytes -= oldest.blob.size;
    _remoteBlobCache.delete(oldestKey);
  }
}

// In-flight syntheses, so a prefetch racing the sequential player cannot
// fire a duplicate network request for the same chunk.
const _remoteInflight = new Map<string, Promise<Blob>>();

async function synthChunk(text: string, signal?: AbortSignal): Promise<Blob> {
  const cached = cacheGet(text);
  if (cached) return cached.blob; // in-session hit: no network at all
  const inflight = _remoteInflight.get(text);
  if (inflight) return inflight;
  const p = (async () => {
    const res = await ttsSynthesize(text, { signal });
    cachePut(text, { blob: res.blob });
    return res.blob;
  })();
  _remoteInflight.set(text, p);
  try {
    return await p;
  } finally {
    _remoteInflight.delete(text);
  }
}

// The currently-playing remote handle, so ttsStop() can also stop <audio>
// playback (not just speechSynthesis).
let _activeRemote: TtsHandle | null = null;

/**
 * Speak through the server-side VoiceStudio engine (docs/17): the text is
 * stripped and chunked exactly like the browser path, each chunk is
 * synthesized over HTTP (`/v1/tts/synthesize`) and played in order through a
 * single <audio> element — one speaker at a time, same generation guard.
 * The next chunk is prefetched while the current one plays, hiding synthesis
 * latency. Privacy: chunk text travels to the configured internal VoiceStudio
 * server.
 */
export function ttsSpeakRemote(text: string, opts: TtsOptions = {}): TtsHandle | null {
  const clean = stripForSpeech(text);
  if (!clean) {
    opts.onEnd?.();
    return null;
  }
  // One speaker at a time: a new remote chain replaces any remote chain in
  // flight (same invariant as ttsSpeak), pausing its <audio> and revoking
  // its blob URL — AND silences any Web Speech voice in flight (the reverse
  // direction of ttsSpeak's cancel of the remote handle).
  _activeRemote?.cancel();
  _activeRemote = null;
  if (ttsIsSupported()) window.speechSynthesis.cancel();
  const chunks = chunkForSpeech(clean);
  const gen = ++_generation;
  let cancelled = false;
  let errorReported = false;
  const reportError = (reason: string) => {
    if (errorReported || cancelled || gen !== _generation) return;
    errorReported = true;
    opts.onError?.(reason);
    // Fallback (docs/17 §2): a remote failure must not leave the user in
    // silence when the browser can speak the remaining text itself.
    if (opts.fallbackToWebSpeech && !cancelled && gen === _generation && ttsIsSupported()) {
      const remaining = chunks.slice(Math.max(0, idx - 1)).join(" ");
      if (remaining.trim()) {
        // The failed chunk is retried by the browser engine: the last chunk
        // handed to synthChunk may or may not have played.
        window.speechSynthesis.cancel();
        const fallbackOpts = { ...opts, fallbackToWebSpeech: undefined };
        ttsSpeak(remaining, fallbackOpts);
      }
    }
  };
  const controller = new AbortController();
  let idx = 0;
  let audio: HTMLAudioElement | null = null;
  let activeUrl: string | null = null;

  const releaseUrl = (url: string) => {
    if (activeUrl === url) activeUrl = null;
    URL.revokeObjectURL(url);
  };

  const playNext = () => {
    if (cancelled || gen !== _generation) return;
    if (idx >= chunks.length) {
      opts.onEnd?.();
      return;
    }
    const chunk = chunks[idx++];
    synthChunk(chunk, controller.signal)
      .then((blob) => {
        if (cancelled || gen !== _generation) return;
        // Prefetch the next chunk while this one plays (still one speaker).
        if (idx < chunks.length) {
          void synthChunk(chunks[idx], controller.signal).catch(() => {});
        }
        const url = URL.createObjectURL(blob);
        activeUrl = url;
        const el = new Audio(url);
        audio = el;
        el.onended = () => {
          releaseUrl(url);
          if (audio === el) audio = null;
          playNext();
        };
        el.onerror = () => {
          releaseUrl(url);
          if (audio === el) audio = null;
          reportError("audio-playback");
        };
        el.play().catch(() => {
          releaseUrl(url);
          reportError("audio-play");
        });
      })
      .catch((err: unknown) => {
        // A failed chunk stops the chain and reports — never silently skip
        // part of the content.
        if (cancelled || gen !== _generation || controller.signal.aborted) return;
        reportError(err instanceof Error ? err.message : String(err));
      });
  };

  opts.onStart?.();
  playNext();
  const handle: TtsHandle = {
    cancel: () => {
      cancelled = true;
      _generation++;
      controller.abort();
      if (audio) {
        audio.pause();
        audio = null;
      }
      if (activeUrl) {
        URL.revokeObjectURL(activeUrl);
        activeUrl = null;
      }
      if (_activeRemote === handle) _activeRemote = null;
    },
  };
  _activeRemote = handle;
  return handle;
}
