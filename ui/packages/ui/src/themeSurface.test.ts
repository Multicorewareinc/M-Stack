import { afterEach, describe, expect, it } from 'vitest';
import {
  applyBackgroundColor,
  applySurfaceColor,
  applyTextColor,
  deriveCanvasColor,
  deriveSurfaceColor,
  deriveTextColor,
} from './themeSurface';

const BG_STYLE_ID = 'multistack-background-override';
const SURFACE_STYLE_ID = 'multistack-surface-override';
const TEXT_STYLE_ID = 'multistack-text-override';

afterEach(() => {
  document.getElementById(BG_STYLE_ID)?.remove();
  document.getElementById(SURFACE_STYLE_ID)?.remove();
  document.getElementById(TEXT_STYLE_ID)?.remove();
});

function channels(triplet: string): number[] {
  return triplet.split(' ').map(Number);
}

/** True if the color is a clear, recognizable tint of some hue — not desaturated down to gray
 * (channels within a few points of each other) or crushed to a corner where hue can't read. */
function looksVisiblyTinted(triplet: string): boolean {
  const c = channels(triplet);
  return Math.max(...c) - Math.min(...c) > 8;
}

describe('deriveCanvasColor', () => {
  it('rejects invalid input', () => {
    expect(deriveCanvasColor('not-a-color', 'dark')).toBeNull();
  });

  it('is darker in dark mode and lighter in light mode, but NOT crushed to near-black/near-white', () => {
    const dark = deriveCanvasColor('#1a2b3c', 'dark')!;
    const light = deriveCanvasColor('#1a2b3c', 'light')!;
    expect(dark).not.toBe(light);
    // Must fail if dark mode isn't actually darker than light mode — that's the whole "theme"
    // distinction this still has to preserve.
    const avg = (t: string) => channels(t).reduce((a, b) => a + b, 0) / 3;
    expect(avg(dark)).toBeLessThan(avg(light));
    // Must fail if either extreme crushes the hue down to gray-black/gray-white — a background
    // customization needs to actually be visible, in both themes, not just dark mode.
    expect(looksVisiblyTinted(dark)).toBe(true);
    expect(looksVisiblyTinted(light)).toBe(true);
  });

  it('keeps a visible tint even for a highly saturated input, in both modes', () => {
    // The exact bug report this guards against: a vivid, fully-saturated color must not wash out
    // to a barely-there pastel in light mode, or crush to indistinguishable near-black in dark mode.
    const dark = deriveCanvasColor('#ce6311', 'dark')!;
    const light = deriveCanvasColor('#ce6311', 'light')!;
    expect(looksVisiblyTinted(dark)).toBe(true);
    expect(looksVisiblyTinted(light)).toBe(true);
  });
});

describe('deriveTextColor', () => {
  it('rejects invalid input', () => {
    expect(deriveTextColor('#zzzzzz', 'light')).toBeNull();
  });

  it('is bright in dark mode and deep in light mode for the same input', () => {
    const dark = deriveTextColor('#1a2b3c', 'dark')!;
    const light = deriveTextColor('#1a2b3c', 'light')!;
    expect(dark).not.toBe(light);
    const darkChannels = dark.split(' ').map(Number);
    expect(darkChannels.every((c) => c > 220)).toBe(true);
    const lightChannels = light.split(' ').map(Number);
    expect(lightChannels.every((c) => c < 60)).toBe(true);
  });
});

describe('deriveSurfaceColor', () => {
  it('rejects invalid input', () => {
    expect(deriveSurfaceColor('not-a-color', 'dark')).toBeNull();
  });

  it('is darker than the canvas in light mode, per explicit product feedback (chrome darker than content)', () => {
    const avg = (t: string) => channels(t).reduce((a, b) => a + b, 0) / 3;
    const lightCanvas = deriveCanvasColor('#1a2b3c', 'light')!;
    const lightSurface = deriveSurfaceColor('#1a2b3c', 'light')!;
    expect(avg(lightSurface)).toBeLessThan(avg(lightCanvas));
  });

  it('in dark mode, prioritizes a visible hue over being darker than the canvas — HSL chroma is capped by lightness, so going darker than deriveCanvasColor\'s already-low 8% would crush any hue to indistinguishable black', () => {
    const dark = deriveSurfaceColor('#c8146b', 'dark')!;
    expect(looksVisiblyTinted(dark)).toBe(true);
  });
});

describe('applyBackgroundColor', () => {
  it('injects a style tag overriding --canvas-bg only, for both root blocks', () => {
    applyBackgroundColor('#0f172a');
    const style = document.getElementById(BG_STYLE_ID);
    expect(style).not.toBeNull();
    expect(style!.textContent).toContain(':root {');
    expect(style!.textContent).toContain(":root[data-theme='light']");
    expect(style!.textContent).toContain('--canvas-bg:');
    // Must fail if it reaches into the Header/Sidebar/card surface variable — that's
    // applySurfaceColor's job, kept independent.
    expect(style!.textContent).not.toContain('--color-neutral-0');
  });

  it('clears the override when given undefined or an invalid color', () => {
    applyBackgroundColor('#0f172a');
    expect(document.getElementById(BG_STYLE_ID)).not.toBeNull();
    applyBackgroundColor(undefined);
    expect(document.getElementById(BG_STYLE_ID)).toBeNull();
  });
});

describe('applySurfaceColor', () => {
  it('injects a style tag overriding --color-neutral-0 only, for both root blocks', () => {
    applySurfaceColor('#7c3aed');
    const style = document.getElementById(SURFACE_STYLE_ID);
    expect(style).not.toBeNull();
    expect(style!.textContent).toContain(':root {');
    expect(style!.textContent).toContain(":root[data-theme='light']");
    expect(style!.textContent).toContain('--color-neutral-0:');
    // Must fail if it reaches into the page canvas variable — independent of applyBackgroundColor.
    expect(style!.textContent).not.toContain('--canvas-bg');
  });

  it('clears the override when given undefined or an invalid color', () => {
    applySurfaceColor('#7c3aed');
    expect(document.getElementById(SURFACE_STYLE_ID)).not.toBeNull();
    applySurfaceColor('garbage');
    expect(document.getElementById(SURFACE_STYLE_ID)).toBeNull();
  });

  it('is independent of applyBackgroundColor — setting one does not touch the other\'s style tag', () => {
    applyBackgroundColor('#0f172a');
    applySurfaceColor('#7c3aed');
    expect(document.getElementById(BG_STYLE_ID)).not.toBeNull();
    expect(document.getElementById(SURFACE_STYLE_ID)).not.toBeNull();
    expect(document.getElementById(BG_STYLE_ID)!.textContent).not.toBe(
      document.getElementById(SURFACE_STYLE_ID)!.textContent,
    );
  });
});

describe('applyTextColor', () => {
  it('injects a style tag overriding --color-neutral-900 for both root blocks', () => {
    applyTextColor('#111827');
    const style = document.getElementById(TEXT_STYLE_ID);
    expect(style).not.toBeNull();
    expect(style!.textContent).toContain(':root {');
    expect(style!.textContent).toContain(":root[data-theme='light']");
    expect(style!.textContent).toContain('--color-neutral-900:');
    // Must fail if it touches secondary/caption text shades — scope is the base shade only.
    expect(style!.textContent).not.toContain('--color-neutral-500');
  });

  it('clears the override when given undefined or an invalid color', () => {
    applyTextColor('#111827');
    expect(document.getElementById(TEXT_STYLE_ID)).not.toBeNull();
    applyTextColor('garbage');
    expect(document.getElementById(TEXT_STYLE_ID)).toBeNull();
  });

  it('re-applying updates the same style element rather than duplicating it', () => {
    applyTextColor('#111827');
    applyTextColor('#334155');
    expect(document.querySelectorAll(`#${TEXT_STYLE_ID}`).length).toBe(1);
  });
});
