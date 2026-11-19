/**
 * Runtime background/text-color overrides — the other knobs in BRANDING.md's customization story,
 * alongside accentColor.ts. Each takes ONE hex color and derives a dark-mode and a light-mode
 * value from it (same one-input, both-modes idea as accentColor), so a fork never has to pick four
 * separate values.
 *
 * Three independent, optional color settings:
 * - `backgroundColor` — overrides only `--canvas-bg` (tokens/index.ts's `canvas`, the backdrop
 *   `body` paints) — the page content area behind Header/Sidebar/cards.
 * - `surfaceColor` — overrides only `--color-neutral-0` (the surface color Header/Sidebar/cards/
 *   panels all use). Header and Sidebar deliberately have no separate setting from each other:
 *   they're chrome around the content and always match one another, but this lets that chrome be
 *   a different color from the content area behind it (`backgroundColor`).
 * - `textColor` — overrides only `--color-neutral-900` (the shade `body`'s base text color and
 *   most headings resolve to). Secondary/caption text (neutral-500/600/700) is untouched.
 *
 * `backgroundColor`/`surfaceColor` get an asymmetric dark/light treatment (see deriveCanvasColor's own doc):
 * dark mode stays low on both lightness AND saturation so it reads as dark first and tinted
 * second; light mode stays clearly saturated and visible rather than washing out to a barely-there
 * pastel. `textColor` is the exception — it stays near the near-black/near-white extreme in both
 * modes, since it needs to read as body text, not a "look at me" surface.
 *
 * Both are opt-in: omitted or invalid input clears any previous override and leaves the shipped
 * default exactly as-is.
 */

import { clamp, hexToRgb, rgbToHsl, hslToRgb, rgbTriplet } from './colorMath';
import { setOverrideStyle } from './styleOverride';

/** Asymmetric on purpose, per explicit product feedback: light mode should be a clearly visible
 * tint (this is the one thing on the page a fork actually wants to *see* — anchoring it near white
 * crushes any hue down to a barely-there pastel that reads as "the customization did nothing").
 * Dark mode is the opposite priority — it must read as an actual DARK theme first, with the hue
 * present only as a subtle tint, not a visibly "colored" (e.g. brown/plum) background. That's why
 * dark mode gets both a lower lightness (8%) AND a lower saturation ceiling (45%) than light mode
 * (82% lightness, up to 90% saturation) — the same input color, but dark mode deliberately holds
 * back on both axes that would make it look bright/saturated instead of dark. */
export function deriveCanvasColor(hex: string, mode: 'dark' | 'light'): string | null {
  const rgb = hexToRgb(hex);
  if (!rgb) return null;
  const [h, s] = rgbToHsl(rgb);
  if (mode === 'light') return rgbTriplet(hslToRgb(h, clamp(s, 40, 90), 82));
  return rgbTriplet(hslToRgb(h, clamp(s, 20, 45), 8));
}

/** Header/Sidebar/card surface color (`--color-neutral-0`), independently settable from
 * deriveCanvasColor's page-backdrop color.
 *
 * In LIGHT mode this reads as darker/moodier than the content area, per explicit product
 * feedback that Header/Sidebar chrome should be darker than the content behind it — there's
 * enough headroom at light-mode lightness levels for a color to be both darker than the canvas
 * AND clearly show its hue.
 *
 * In DARK mode that "darker than an already-dark canvas" goal is physically impossible to combine
 * with "visibly colored": deriveCanvasColor's dark canvas already sits at 8% lightness, and HSL
 * chroma is capped at roughly `2 * lightness` — going any darker than that leaves no room for any
 * hue to show at all, at ANY saturation (tried at 2% and 5% lightness; both were indistinguishable
 * from plain black). Per explicit product feedback, dark mode prioritizes the color actually being
 * visible over strictly being darker than the canvas — so it uses a notably higher lightness
 * (18%) than deriveCanvasColor's 8%, trading "always darker" for "always recognizably the chosen
 * hue." */
export function deriveSurfaceColor(hex: string, mode: 'dark' | 'light'): string | null {
  const rgb = hexToRgb(hex);
  if (!rgb) return null;
  const [h, s] = rgbToHsl(rgb);
  if (mode === 'light') return rgbTriplet(hslToRgb(h, clamp(s, 55, 95), 68));
  return rgbTriplet(hslToRgb(h, clamp(s, 35, 65), 18));
}

/** Opposite direction from the canvas: bright in dark mode (readable on a dark backdrop), deep
 * in light mode (readable on a light one) — same direction as accentColor's 700. Same
 * saturation-clamping rationale as deriveCanvasColor above. */
export function deriveTextColor(hex: string, mode: 'dark' | 'light'): string | null {
  const rgb = hexToRgb(hex);
  if (!rgb) return null;
  const [h, s] = rgbToHsl(rgb);
  if (mode === 'light') return rgbTriplet(hslToRgb(h, clamp(s, 15, 70), 15));
  return rgbTriplet(hslToRgb(h, clamp(s, 15, 60), 92));
}

const BACKGROUND_STYLE_ID = 'multistack-background-override';

/** Applies (or clears) a fork's custom page content background color — reaches only the page
 * canvas (`--canvas-bg`), not Header/Sidebar/cards (see applySurfaceColor for those). Call once
 * at startup with `BRANDING.backgroundColor` (see accentColor.ts's applyAccentColor for the same
 * pattern). */
export function applyBackgroundColor(hex: string | undefined): void {
  const dark = hex ? deriveCanvasColor(hex, 'dark') : null;
  const light = hex ? deriveCanvasColor(hex, 'light') : null;
  if (!dark || !light) {
    setOverrideStyle(BACKGROUND_STYLE_ID, null);
    return;
  }
  setOverrideStyle(
    BACKGROUND_STYLE_ID,
    [`:root {\n  --canvas-bg: ${dark};\n}`, `:root[data-theme='light'] {\n  --canvas-bg: ${light};\n}`].join('\n'),
  );
}

const SURFACE_STYLE_ID = 'multistack-surface-override';

/** Applies (or clears) a fork's custom Header/Sidebar/card surface color — reaches only
 * `--color-neutral-0`, independent of applyBackgroundColor's page canvas. Call once at startup
 * with `BRANDING.surfaceColor` (see accentColor.ts's applyAccentColor for the same pattern). */
export function applySurfaceColor(hex: string | undefined): void {
  const dark = hex ? deriveSurfaceColor(hex, 'dark') : null;
  const light = hex ? deriveSurfaceColor(hex, 'light') : null;
  if (!dark || !light) {
    setOverrideStyle(SURFACE_STYLE_ID, null);
    return;
  }
  setOverrideStyle(
    SURFACE_STYLE_ID,
    [
      `:root {\n  --color-neutral-0: ${dark};\n}`,
      `:root[data-theme='light'] {\n  --color-neutral-0: ${light};\n}`,
    ].join('\n'),
  );
}

const TEXT_STYLE_ID = 'multistack-text-override';

/** Applies (or clears) a fork's custom base text color. Call once at startup with
 * `BRANDING.textColor` (see accentColor.ts's applyAccentColor for the same pattern). */
export function applyTextColor(hex: string | undefined): void {
  const dark = hex ? deriveTextColor(hex, 'dark') : null;
  const light = hex ? deriveTextColor(hex, 'light') : null;
  if (!dark || !light) {
    setOverrideStyle(TEXT_STYLE_ID, null);
    return;
  }
  setOverrideStyle(
    TEXT_STYLE_ID,
    [
      `:root {\n  --color-neutral-900: ${dark};\n}`,
      `:root[data-theme='light'] {\n  --color-neutral-900: ${light};\n}`,
    ].join('\n'),
  );
}
