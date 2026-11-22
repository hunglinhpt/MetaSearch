/**
 * Theme switching (dark / light / system) — UI parity follow-up for docs/33.
 *
 * The document element carries data-theme="dark" | "light" | "system".
 * CSS resolves "system" through prefers-color-scheme media queries, so OS-level
 * theme changes apply live without any JS listener. The user's choice persists
 * in localStorage; the layout's inline pre-paint script applies the stored
 * value before first paint to avoid a flash of the wrong theme.
 */

export type ThemeChoice = "system" | "dark" | "light";
export type ResolvedTheme = "dark" | "light";

export const THEME_STORAGE_KEY = "metasearch.theme";

/** Stable cycle order for the toggle button: system → dark → light → system. */
export const THEME_CHOICES: readonly ThemeChoice[] = ["system", "dark", "light"];

export const THEME_ICONS: Record<ThemeChoice, string> = {
  system: "🖥️",
  dark: "🌙",
  light: "☀️",
};

export const THEME_LABELS: Record<ThemeChoice, string> = {
  system: "Tự động theo hệ thống",
  dark: "Giao diện tối",
  light: "Giao diện sáng",
};

export function isValidThemeChoice(value: string | null): value is ThemeChoice {
  return value === "system" || value === "dark" || value === "light";
}

/** Resolve the palette that actually renders for a choice under an OS preference. */
export function resolveTheme(choice: ThemeChoice, prefersDark: boolean): ResolvedTheme {
  if (choice === "system") return prefersDark ? "dark" : "light";
  return choice;
}

/** Read + validate the stored choice; default (and SSR-safe) is "system". */
export function readStoredTheme(): ThemeChoice {
  if (typeof window === "undefined") return "system";
  try {
    const raw = window.localStorage.getItem(THEME_STORAGE_KEY);
    return isValidThemeChoice(raw) ? raw : "system";
  } catch {
    return "system"; // private mode / storage disabled
  }
}

/** Persist the choice; silent when storage is unavailable (session-only). */
export function storeTheme(choice: ThemeChoice): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, choice);
  } catch {
    // storage unavailable — choice applies for this session only
  }
}

/** Next choice in the stable cycle order (safe for unknown inputs). */
export function cycleTheme(current: ThemeChoice): ThemeChoice {
  const idx = THEME_CHOICES.indexOf(current);
  return THEME_CHOICES[(idx + 1) % THEME_CHOICES.length];
}

/** Write data-theme on <html>. No-op during SSR. */
export function applyThemeToDocument(choice: ThemeChoice): void {
  if (typeof document === "undefined") return;
  document.documentElement.dataset.theme = choice;
}
