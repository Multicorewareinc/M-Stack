import type { Config } from 'tailwindcss';
import {
  borders,
  breakpoints,
  colors,
  motion,
  radius,
  shadows,
  spacing,
  typography,
  zIndex,
} from './index';

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
        neutral: colors.neutral,
        primary: colors.primary,
        success: colors.success,
        warning: colors.warning,
        danger: colors.danger,
        info: colors.info,
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
      borderColor: borders.color,
      boxShadow: shadows,
      zIndex: Object.fromEntries(Object.entries(zIndex).map(([k, v]) => [k, String(v)])),
      screens: breakpoints,
      transitionDuration: motion.duration,
      transitionTimingFunction: motion.easing,
    },
  },
  plugins: [],
};

export default multistackPreset;
