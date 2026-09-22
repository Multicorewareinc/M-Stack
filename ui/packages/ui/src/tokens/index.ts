/**
 * Design tokens — the single source of visual truth for @multistack/ui.
 * Components derive every visual value from these; the Tailwind preset
 * (./preset) projects them into the utility theme. No component hard-codes
 * a color/spacing/radius/z-index literal (see no-hardcoded-values.test.ts).
 *
 * Direction: quiet, dense, modern developer-console (blueprint §6–7, §15).
 *
 * Palette: "Precision Engine" dark theme (instrumental, high-density,
 * developer-console aesthetic). The neutral ramp is inverted relative to a
 * light theme — 0 is the darkest base surface, 900 the brightest text — so
 * every existing `bg-neutral-0` / `text-neutral-900` usage across components
 * and pages repaints automatically with no markup changes.
 */

export const colors = {
  // Neutral ramp (slate-ish), the workhorse of the UI. 0 = darkest surface,
  // 900 = brightest text.
  neutral: {
    0: '#0e131a',
    50: '#161d26',
    100: '#1b2027',
    200: '#2e3846',
    300: '#3b4758',
    400: '#64748b',
    500: '#94a3b8',
    600: '#aab4c5',
    700: '#c2c6d6',
    800: '#e2e8f0',
    900: '#f8fafc',
  },
  // Restrained brand accent.
  primary: {
    50: '#111d2f',
    100: '#16283f',
    500: '#3b82f6',
    600: '#2563eb',
    700: '#93c5fd',
  },
  // Semantic status colors (paired with icon/shape, never color-only).
  success: { 100: '#0f2a22', 500: '#10b981', 700: '#6ee7b7' },
  warning: { 100: '#2c2210', 500: '#f59e0b', 700: '#fcd34d' },
  danger: { 100: '#2f1518', 500: '#ef4444', 700: '#fca5a5' },
  info: { 100: '#122a3d', 500: '#38bdf8', 700: '#7dd3fc' },
} as const;

export const typography = {
  fontFamily: {
    sans: 'Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif',
    mono: '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
  },
  fontSize: {
    caption: '0.75rem',
    secondary: '0.8125rem',
    body: '0.875rem',
    cardTitle: '1rem',
    sectionTitle: '1.125rem',
    pageTitle: '1.5rem',
  },
  fontWeight: { normal: '400', medium: '500', semibold: '600' },
  lineHeight: { tight: '1.25', normal: '1.5' },
} as const;

// Spacing scale (px) — blueprint §15.
export const spacing = {
  0: '0px',
  1: '4px',
  2: '8px',
  3: '12px',
  4: '16px',
  5: '20px',
  6: '24px',
  8: '32px',
  10: '40px',
  12: '48px',
  16: '64px',
} as const;

export const radius = {
  none: '0px',
  sm: '4px',
  md: '6px',
  lg: '8px',
  xl: '12px',
  full: '9999px',
} as const;

export const borders = {
  width: { none: '0px', hairline: '1px', thick: '2px' },
  color: { subtle: colors.neutral[200], default: colors.neutral[300], strong: colors.neutral[400] },
} as const;

export const shadows = {
  none: 'none',
  sm: '0 1px 2px 0 rgb(0 0 0 / 0.3)',
  md: '0 8px 24px -4px rgb(0 0 0 / 0.45)',
  lg: '0 16px 40px -8px rgb(0 0 0 / 0.7)',
} as const;

export const zIndex = {
  base: 0,
  dropdown: 1000,
  sticky: 1100,
  overlay: 1200,
  modal: 1300,
  toast: 1400,
} as const;

export const breakpoints = {
  sm: '640px',
  md: '768px',
  lg: '1024px',
  xl: '1280px',
  '2xl': '1440px',
} as const;

export const motion = {
  duration: { fast: '120ms', base: '180ms', slow: '240ms' },
  easing: { standard: 'cubic-bezier(0.2, 0, 0, 1)' },
} as const;

export const tokens = {
  colors,
  typography,
  spacing,
  radius,
  borders,
  shadows,
  zIndex,
  breakpoints,
  motion,
} as const;

export type Tokens = typeof tokens;
