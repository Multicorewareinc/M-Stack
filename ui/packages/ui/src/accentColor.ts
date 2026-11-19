/**
 * Runtime accent-color override for forks that want their own brand color without touching this
 * package's source (see ../BRANDING.md). The shipped "Precision Engine" palette hardcodes a blue
 * accent as `--color-primary-*` CSS variables (tokens/index.ts, projected in styles/globals.css);
 * this drives every "selected/highlighted" surface across the app — the active Sidebar nav item,
 * solid Button/Tab/Select-option backgrounds, focus rings, and (via the `::selection` rule in
 * globals.css) selected text — so overriding just this one variable group re-themes all of them
 * at once, in both dark and light mode, with no rebuild.
 *
 * Given ONE hex color, `applyAccentColor` derives the same five shades (50/100/500/600/700) the
 * shipped palette has, separately for dark and light mode (their `50`/`100`/`700` intentionally
 * differ in direction — see tokens/index.ts's module doc), and injects them as a `<style>` tag
 * that overrides `globals.css`'s defaults by source order. Passing no color (or an invalid one) is
 * a no-op, leaving the shipped default exactly as-is — this is opt-in, not a replacement for the
 * default theme.
 */

import { clamp, hexToRgb, rgbToHsl, hslToRgb, rgbTriplet } from './colorMath';
import { setOverrideStyle } from './styleOverride';

export interface AccentShades {
  50: string;
  100: string;
  500: string;
  600: string;
  700: string;
}

/** Derives the five primary shades for one theme mode from a single base accent color, mirroring
 * the shape (though not the exact values) of tokens/index.ts's shipped `colors.primary` /
 * `lightColors.primary`. `700` and `50`/`100` deliberately go opposite directions between modes —
 * `700` is a light/bright tint in dark mode (readable text on a dark surface) and a deep shade in
 * light mode (readable text on a light surface); `50`/`100` are near-black tints in dark mode and
 * near-white tints in light mode. */
export function deriveAccentShades(hex: string, mode: 'dark' | 'light'): AccentShades | null {
  const rgb = hexToRgb(hex);
  if (!rgb) return null;
  const [h, s, l] = rgbToHsl(rgb);
  if (mode === 'light') {
    return {
      500: rgbTriplet(hslToRgb(h, s, l)),
      600: rgbTriplet(hslToRgb(h, s, clamp(l - 10, 0, 100))),
      700: rgbTriplet(hslToRgb(h, s, clamp(l - 20, 0, 100))),
      50: rgbTriplet(hslToRgb(h, clamp(s + 5, 0, 100), 97)),
      100: rgbTriplet(hslToRgb(h, clamp(s + 5, 0, 100), 93)),
    };
  }
  return {
    500: rgbTriplet(hslToRgb(h, s, l)),
    600: rgbTriplet(hslToRgb(h, s, clamp(l - 10, 0, 100))),
    700: rgbTriplet(hslToRgb(h, clamp(s - 10, 0, 100), clamp(l + 22, 0, 92))),
    50: rgbTriplet(hslToRgb(h, clamp(s - 30, 20, 100), 12)),
    100: rgbTriplet(hslToRgb(h, clamp(s - 25, 20, 100), 16)),
  };
}

const STYLE_ELEMENT_ID = 'multistack-accent-override';

function shadesToCssBlock(selector: string, shades: AccentShades): string {
  return `${selector} {\n${(['50', '100', '500', '600', '700'] as const)
    .map((shade) => `  --color-primary-${shade}: ${shades[shade]};`)
    .join('\n')}\n}`;
}

/**
 * Applies (or clears) a fork's custom accent color at runtime. Call once at startup (see each
 * portal's main.tsx) with `BRANDING.accentColor`. Safe to call with `undefined`/an invalid hex —
 * it removes any previous override and leaves the shipped default palette untouched, so this is
 * entirely opt-in.
 */
export function applyAccentColor(hex: string | undefined): void {
  const dark = hex ? deriveAccentShades(hex, 'dark') : null;
  const light = hex ? deriveAccentShades(hex, 'light') : null;
  if (!dark || !light) {
    setOverrideStyle(STYLE_ELEMENT_ID, null);
    return;
  }
  setOverrideStyle(
    STYLE_ELEMENT_ID,
    [shadesToCssBlock(':root', dark), shadesToCssBlock(":root[data-theme='light']", light)].join('\n'),
  );
}
