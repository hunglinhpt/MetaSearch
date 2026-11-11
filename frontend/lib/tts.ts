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

// Generation counter: any new speak/stop invalidates every chain from older
// generations, so an engine that fires `onend` after an external
// `synth.cancel()` (Firefox/Safari) cannot resume a stale chain behind a new
// speaker.
let _generation = 0;

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
};

export type TtsHandle = { cancel: () => void };

export function ttsSpeak(text: string, opts: TtsOptions = {}): TtsHandle | null {
  if (!ttsIsSupported()) {
    opts.onError?.("unsupported");
    return null;
  }
  const synth = window.speechSynthesis;
  synth.cancel(); // one speaker at a time — replaces anything in flight
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
  if (ttsIsSupported()) {
    _generation++;
    window.speechSynthesis.cancel();
  }
}
