// Theme engine ("midnight terminal" dark ↔ "daybreak" light, docs/09):
// owns the `dark` class on <html>, persists the choice under localStorage
// "theme", and defaults to saved → prefers-color-scheme → dark. index.html
// carries a matching inline snippet so FIRST PAINT already has the right
// variable scope (no FOUC) before React mounts; this provider takes over
// from there and keeps <html> in sync on every change.
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { ReactNode } from "react";

export type Theme = "dark" | "light";

const STORAGE_KEY = "theme";

/** Null when nothing (or something invalid) is persisted — OS decides. */
function readStoredTheme(): Theme | null {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    return saved === "light" || saved === "dark" ? saved : null;
  } catch {
    return null;
  }
}

function initialTheme(): Theme {
  const saved = readStoredTheme();
  if (saved !== null) return saved;
  // Dark stays the hero theme; light only wins when the OS asks for it.
  return window.matchMedia("(prefers-color-scheme: light)").matches
    ? "light"
    : "dark";
}

interface ThemeContextValue {
  theme: Theme;
  setTheme: (theme: Theme) => void;
  toggleTheme: () => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(initialTheme);

  // Persisted choice is sticky: once pinned it outranks the OS preference.
  const setTheme = useCallback((next: Theme) => {
    setThemeState(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Private mode / storage disabled — theme still applies for the session.
    }
  }, []);

  const toggleTheme = useCallback(() => {
    setTheme(theme === "dark" ? "light" : "dark");
  }, [setTheme, theme]);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
  }, [theme]);

  // M72 (docs/09 §Installable PWA): the installed/standalone window chrome
  // paints `theme-color`, so it tracks the active theme instead of staying on
  // the dark default the document ships with.
  useEffect(() => {
    const meta = document.querySelector<HTMLMetaElement>(
      'meta[name="theme-color"]',
    );
    if (meta) meta.content = theme === "dark" ? "#101216" : "#f6f4ef";
  }, [theme]);

  // Until the visitor pins a choice, live-follow the OS preference.
  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: light)");
    const onChange = (event: MediaQueryListEvent) => {
      if (readStoredTheme() === null) {
        setThemeState(event.matches ? "light" : "dark");
      }
    };
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);

  const value = useMemo<ThemeContextValue>(
    () => ({ theme, setTheme, toggleTheme }),
    [theme, setTheme, toggleTheme],
  );
  return (
    <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
  );
}

/** Current theme + switchers. Charts re-derive their palette from this. */
export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (ctx === null) {
    throw new Error("useTheme must be used inside <ThemeProvider>");
  }
  return ctx;
}
