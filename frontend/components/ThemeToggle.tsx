"use client";

/**
 * Theme toggle (dark / light / system) — compact segmented control in the masthead.
 *
 * - The choice is written to <html data-theme> and localStorage; layout's
 *   inline pre-paint script re-applies it on the next page load before paint.
 * - "system" resolves in pure CSS (prefers-color-scheme), so OS theme changes
 *   apply live with zero listeners; this component only tracks the *choice*.
 * - Also syncs if the user changes the theme in another tab (storage event).
 */

import { useCallback, useEffect, useState } from "react";

import {
  THEME_ICONS,
  THEME_LABELS,
  THEME_STORAGE_KEY,
  applyThemeToDocument,
  readStoredTheme,
  storeTheme,
  type ThemeChoice,
} from "../lib/theme";

const MODES: readonly ThemeChoice[] = ["system", "dark", "light"];

export default function ThemeToggle() {
  const [theme, setTheme] = useState<ThemeChoice>("system");

  useEffect(() => {
    // The pre-paint script already set data-theme; adopt it without a repaint.
    const initial = readStoredTheme();
    setTheme(initial);
    applyThemeToDocument(initial);
  }, []);

  useEffect(() => {
    const onStorage = (ev: StorageEvent) => {
      if (ev.key !== THEME_STORAGE_KEY) return;
      const next = readStoredTheme();
      setTheme(next);
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const pick = useCallback((next: ThemeChoice) => {
    setTheme(next);
    applyThemeToDocument(next);
    storeTheme(next);
  }, []);

  return (
    <div className="theme-toggle" role="group" aria-label="Chủ đề giao diện">
      {MODES.map((mode) => (
        <button
          key={mode}
          type="button"
          className={`theme-toggle-btn ${theme === mode ? "active" : ""}`}
          aria-pressed={theme === mode}
          aria-label={THEME_LABELS[mode]}
          title={THEME_LABELS[mode]}
          onClick={() => pick(mode)}
        >
          <span aria-hidden="true">{THEME_ICONS[mode]}</span>
        </button>
      ))}
    </div>
  );
}
