/**
 * Vietnamese dictation (speech → query text) for the MetaSearch Web UI.
 *
 * Engine: the browser's SpeechRecognition API (Chrome/Edge; webkit-prefixed
 * historically). Same degradation contract as lib/tts.ts: unsupported
 * browsers get `dictationIsSupported() === false` and callers hide the mic.
 * Privacy: like TTS, Chrome's recognizer processes audio in the cloud — the
 * caller surfaces the same online-voice disclosure.
 */

export const DICTATION_LANG = "vi-VN";

/** Raw SpeechRecognition error codes → user-actionable reasons (VN UI). */
export function dictationErrorReason(code: string): string {
  switch (code) {
    case "not-allowed":
    case "service-not-allowed":
      return "permission";
    case "no-speech":
      return "no-speech";
    case "network":
      return "network";
    case "audio-capture":
      return "no-microphone";
    default:
      return code || "unknown";
  }
}

/**
 * Pure transcript merge: committed final chunks + the live interim preview.
 * `finalSoFar` is everything already committed (ends without separator);
 * `newFinal` is a fresh committed chunk; `interim` is the unstable tail.
 * Used by tests to pin the merge semantics without a real recognizer.
 */
export function mergeTranscripts(
  finalSoFar: string,
  newFinal: string | null,
  interim: string | null,
): string {
  const parts = [finalSoFar.trim()];
  if (newFinal && newFinal.trim()) parts.push(newFinal.trim());
  const committed = parts.filter(Boolean).join(" ");
  if (interim && interim.trim()) {
    return committed ? `${committed} ${interim.trim()}` : interim.trim();
  }
  return committed;
}

export function dictationIsSupported(): boolean {
  if (typeof window === "undefined") return false;
  const w = window as unknown as Record<string, unknown>;
  return typeof w.SpeechRecognition === "function" || typeof w.webkitSpeechRecognition === "function";
}

export type DictationOptions = {
  lang?: string;
  /** Fires on every result: full committed text + live interim tail. */
  onUpdate: (committed: string, interim: string) => void;
  /** Recognition ended (silence, manual stop, or error). */
  onEnd?: (reason: string) => void;
};

export type DictationHandle = { stop: () => void };

export function startDictation(opts: DictationOptions): DictationHandle | null {
  if (!dictationIsSupported()) {
    opts.onEnd?.("unsupported");
    return null;
  }
  const w = window as unknown as Record<string, new () => SpeechRecognitionLike>;
  const Ctor = (w.SpeechRecognition ?? w.webkitSpeechRecognition) as new () => SpeechRecognitionLike;
  const recognition = new Ctor();
  recognition.lang = opts.lang ?? DICTATION_LANG;
  recognition.continuous = true;
  recognition.interimResults = true;

  let stopped = false;
  let ended = false; // onerror + onend can BOTH fire for one failure — report once
  let finalSoFar = "";
  // Audit round 9: Chrome may re-deliver already-seen results with
  // continuous=true — a processed-index watermark prevents double-merging.
  let processedUpTo = 0;
  // docs/18 A1: auto-stop after ~1.2s of silence (reset on every result).
  const SILENCE_MS = 1200;
  let silenceTimer: ReturnType<typeof setTimeout> | null = null;
  const armSilence = () => {
    if (silenceTimer) clearTimeout(silenceTimer);
    silenceTimer = setTimeout(() => {
      try {
        recognition.stop(); // engine onend fires -> natural finish path
      } catch {
        // already stopped by the engine
      }
    }, SILENCE_MS);
  };
  const disarmSilence = () => {
    if (silenceTimer) {
      clearTimeout(silenceTimer);
      silenceTimer = null;
    }
  };

  const finish = (reason: string) => {
    if (ended) return;
    ended = true;
    opts.onUpdate(finalSoFar, "");
    opts.onEnd?.(reason);
  };

  recognition.onresult = (event: SpeechRecognitionEventLike) => {
    let interim = "";
    for (let i = processedUpTo; i < event.results.length; i++) {
      const result = event.results[i];
      const text = result[0]?.transcript ?? "";
      if (result.isFinal) {
        finalSoFar = mergeTranscripts(finalSoFar, text, null);
      } else {
        interim += text;
      }
    }
    processedUpTo = Math.max(processedUpTo, event.results.length);
    armSilence();
    opts.onUpdate(finalSoFar, interim.trim());
  };

  recognition.onerror = (event: { error?: string }) => {
    disarmSilence();
    if (stopped) return; // our own stop() can surface "aborted"
    finish(dictationErrorReason(event.error ?? ""));
  };

  recognition.onend = () => {
    disarmSilence();
    if (stopped) return;
    // Natural stop (silence / server cut): report the accumulated text once.
    finish("finished");
  };

  try {
    recognition.start();
  } catch {
    opts.onEnd?.("start-failed");
    return null;
  }

  return {
    stop: () => {
      if (stopped) return;
      stopped = true;
      disarmSilence();
      try {
        recognition.stop();
      } catch {
        // already stopped by the engine
      }
      finish("stopped");
    },
  };
}

/* Minimal structural types — the DOM lib may not ship SpeechRecognition. */
type SpeechRecognitionLike = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  start: () => void;
  stop: () => void;
  onresult: ((event: SpeechRecognitionEventLike) => void) | null;
  onerror: ((event: { error?: string }) => void) | null;
  onend: (() => void) | null;
};

type SpeechRecognitionEventLike = {
  resultIndex: number;
  results: ArrayLike<{ isFinal: boolean; 0?: { transcript?: string }; length: number }>;
};
