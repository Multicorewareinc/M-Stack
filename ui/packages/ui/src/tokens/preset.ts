import type { Config } from 'tailwindcss';
import {
  borders,
  breakpoints,
  colors,
  motion,
  radius,
  spacing,
  typography,
  zIndex,
} from './index';

/**
 * Projects a color token group's shade keys onto CSS custom-property references, in Tailwind's
 * `rgb(var(...) / <alpha-value>)` form — this is what lets `bg-neutral-0/50`-style opacity
 * modifiers keep working. `colors` (the dark palette) supplies only the *shape* (which groups,
 * which shades exist) here; the actual color VALUES for both themes live in `globals.css` as
 * `--color-<group>-<shade>` variables (dark under `:root`, light under `:root[data-theme=light]`),
 * so a theme swap repaints via CSS with no rebuild. See ../theme.tsx for the runtime toggle and
 * theme-css-vars.test.ts for the cross-check that globals.css matches `colors`/`lightColors`.
 */
function cssVarColorGroup<T extends Record<string, string>>(group: string, shades: T): Record<keyof T, string> {
  const result = {} as Record<keyof T, string>;
  for (const shade of Object.keys(shades) as (keyof T)[]) {
    result[shade] = `rgb(var(--color-${group}-${String(shade)}) / <alpha-value>)`;
  }
  return result;
}

const neutralVars = cssVarColorGroup('neutral', colors.neutral);

/**
 * `border-subtle` / `border-default` / `border-strong` are semantic aliases for specific neutral
 * shades (borders.color in tokens/index.ts) — reusing the SAME `--color-neutral-*` variables the
 * neutral color scale already projects (rather than inventing separate `--border-*` variables)
 * keeps them theme-aware for free and guarantees they can never drift from the neutral ramp.
 */
const borderColorVars = {
  subtle: neutralVars[200 as keyof typeof neutralVars],
  default: neutralVars[300 as keyof typeof neutralVars],
  strong: neutralVars[400 as keyof typeof neutralVars],
};

/**
 * `shadow-sm/md/lg` as CSS-variable references — `--shadow-*` (globals.css) holds the dark values
 * under `:root` and much lower-alpha light values under `:root[data-theme=light]` (see
 * `shadows`/`lightShadows` in tokens/index.ts): a shadow tuned for a near-black surface reads as a
 * heavy smudge on white, so this needs the same per-theme treatment colors get, not a shared value.
 */
const boxShadowVars = { none: 'none', sm: 'var(--shadow-sm)', md: 'var(--shadow-md)', lg: 'var(--shadow-lg)' };

/**
 * Tailwind preset that projects the design tokens into the utility theme.
 * Both portals extend this preset so tokens are the single source of truth.
 * Values are referenced from ./index — never re-literalized here
 * (asserted by preset.test.ts).
 */
export const multistackPreset: Config = {
  content: [],
  theme: {
    extend: {
      colors: {
        neutral: cssVarColorGroup('neutral', colors.neutral),
        primary: cssVarColorGroup('primary', colors.primary),
        success: cssVarColorGroup('success', colors.success),
        warning: cssVarColorGroup('warning', colors.warning),
        danger: cssVarColorGroup('danger', colors.danger),
        info: cssVarColorGroup('info', colors.info),
      },
      fontFamily: {
        sans: typography.fontFamily.sans.split(', '),
        mono: typography.fontFamily.mono.split(', '),
      },
      fontSize: typography.fontSize,
      fontWeight: typography.fontWeight,
      spacing,
      borderRadius: radius,
      borderWidth: borders.width,
      borderColor: borderColorVars,
      boxShadow: boxShadowVars,
      zIndex: Object.fromEntries(Object.entries(zIndex).map(([k, v]) => [k, String(v)])),
      screens: breakpoints,
      transitionDuration: motion.duration,
      transitionTimingFunction: motion.easing,
    },
  },
  plugins: [],
};

export default multistackPreset;
