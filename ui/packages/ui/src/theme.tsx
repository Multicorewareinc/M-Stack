import * as React from 'react';

export type Theme = 'light' | 'dark';

const STORAGE_KEY = 'multistack-theme';

interface ThemeContextValue {
  theme: Theme;
  setTheme: (theme: Theme) => void;
  toggleTheme: () => void;
}

const ThemeContext = React.createContext<ThemeContextValue | undefined>(undefined);

function readStoredTheme(): Theme | null {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    return stored === 'light' || stored === 'dark' ? stored : null;
  } catch {
    // Private browsing / storage blocked — fall through to the default theme.
    return null;
  }
}

function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
}

export interface ThemeProviderProps {
  children: React.ReactNode;
  /** Theme used when no stored preference exists yet. This design system ships dark-first. */
  defaultTheme?: Theme;
}

/**
 * Applies `theme` as `data-theme` on `<html>` — `globals.css` defines the dark palette on
 * `:root` and overrides it under `:root[data-theme="light"]`, so this single attribute repaints
 * every `bg-neutral-0` / `text-neutral-900` etc. usage across both portals with no per-component
 * change (see tokens/index.ts). Persists to localStorage so the choice survives a reload; an
 * inline script in each portal's index.html applies the stored value before React mounts to
 * avoid a flash of the wrong theme.
 */
export function ThemeProvider({ children, defaultTheme = 'dark' }: ThemeProviderProps) {
  const [theme, setThemeState] = React.useState<Theme>(() => readStoredTheme() ?? defaultTheme);

  React.useEffect(() => {
    applyTheme(theme);
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // Storage blocked: the theme still applies for this session, it just won't persist.
    }
  }, [theme]);

  const setTheme = React.useCallback((next: Theme) => setThemeState(next), []);
  const toggleTheme = React.useCallback(() => setThemeState((t) => (t === 'dark' ? 'light' : 'dark')), []);

  const value = React.useMemo(() => ({ theme, setTheme, toggleTheme }), [theme, setTheme, toggleTheme]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const ctx = React.useContext(ThemeContext);
  if (!ctx) throw new Error('useTheme must be used within a ThemeProvider');
  return ctx;
}
