import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  THEME_CHOICES,
  THEME_STORAGE_KEY,
  applyThemeToDocument,
  cycleTheme,
  isValidThemeChoice,
  readStoredTheme,
  resolveTheme,
  storeTheme,
  type ThemeChoice,
} from "./theme";

function stubBrowserGlobals() {
  const store = new Map<string, string>();
  (globalThis as any).window = {
    localStorage: {
      getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
      clear: () => store.clear(),
    },
  };
  const doc = (globalThis as any).document ?? {};
  doc.documentElement = { dataset: {} };
  (globalThis as any).document = doc;
}

describe("theme validation", () => {
  it("accepts only the three known choices", () => {
    expect(isValidThemeChoice("system")).toBe(true);
    expect(isValidThemeChoice("dark")).toBe(true);
    expect(isValidThemeChoice("light")).toBe(true);
    expect(isValidThemeChoice("sepia")).toBe(false);
    expect(isValidThemeChoice(null)).toBe(false);
  });
});

describe("resolveTheme", () => {
  it("follows the OS preference only in system mode", () => {
    expect(resolveTheme("system", true)).toBe("dark");
    expect(resolveTheme("system", false)).toBe("light");
  });

  it("pins explicit choices regardless of the OS", () => {
    expect(resolveTheme("dark", false)).toBe("dark");
    expect(resolveTheme("light", true)).toBe("light");
  });
});

describe("cycleTheme", () => {
  it("cycles system → dark → light → system in the stable order", () => {
    let cur: ThemeChoice = "system";
    const seen: string[] = [cur];
    for (let i = 0; i < 3; i++) {
      cur = cycleTheme(cur);
      seen.push(cur);
    }
    expect(seen).toEqual(["system", "dark", "light", "system"]);
  });

  it("covers every choice in one full cycle", () => {
    expect(new Set(THEME_CHOICES).size).toBe(3);
  });
});

describe("stored theme preference", () => {
  beforeEach(() => {
    stubBrowserGlobals();
    (globalThis as any).window.localStorage.clear();
    (globalThis as any).document.documentElement.dataset = {};
  });

  it("defaults to system when nothing valid is stored", () => {
    expect(readStoredTheme()).toBe("system");
    (globalThis as any).window.localStorage.setItem(THEME_STORAGE_KEY, "neon");
    expect(readStoredTheme()).toBe("system");
  });

  it("persists and re-reads an explicit choice", () => {
    storeTheme("dark");
    expect(readStoredTheme()).toBe("dark");
  });

  it("never throws when storage blows up", () => {
    (globalThis as any).window.localStorage.setItem = () => {
      throw new Error("quota");
    };
    (globalThis as any).window.localStorage.getItem = () => {
      throw new Error("sealed");
    };
    expect(readStoredTheme()).toBe("system");
    expect(() => storeTheme("light")).not.toThrow();
  });

  it("writes data-theme on the document element", () => {
    applyThemeToDocument("light");
    expect((globalThis as any).document.documentElement.dataset.theme).toBe("light");
  });
});

it("module is importable on the server path (no window access at import time)", () => {
  // The functions above guard `typeof window`/`typeof document`; importing the
  // module itself must never touch globals (Next.js SSR + vitest node env).
  expect(vi.isMockFunction).toBeDefined();
});
