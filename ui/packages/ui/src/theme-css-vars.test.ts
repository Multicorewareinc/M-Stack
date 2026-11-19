import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { canvas, colors, lightColors, lightShadows, shadows } from './tokens/index';

const css = readFileSync(resolve(process.cwd(), 'src/styles/globals.css'), 'utf8');

function hexToRgbTriplet(hex: string): string {
  const h = hex.replace('#', '');
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `${r} ${g} ${b}`;
}

/** Extracts the `--name: r g b;` declaration nearest a given anchor line (used to disambiguate
 * the dark `:root` block from the light `:root[data-theme='light']` block below it). */
function readVar(name: string, from: number): string | null {
  const re = new RegExp(`--${name}:\\s*([0-9]+ [0-9]+ [0-9]+)\\s*;`);
  const match = css.slice(from).match(re);
  return match ? match[1] : null;
}

/** Same as readVar, but for a free-form value (e.g. a box-shadow string) instead of an RGB triplet. */
function readFreeformVar(name: string, from: number): string | null {
  const re = new RegExp(`--${name}:\\s*([^;]+);`);
  const match = css.slice(from).match(re);
  return match ? match[1].trim() : null;
}

describe('globals.css theme variables match tokens/index.ts', () => {
  const lightBlockStart = css.indexOf(":root[data-theme='light']");

  it('has a light theme block', () => {
    expect(lightBlockStart).toBeGreaterThan(0);
  });

  it('dark block matches `colors` exactly, shade for shade', () => {
    for (const [group, shades] of Object.entries(colors)) {
      for (const [shade, hex] of Object.entries(shades)) {
        // Must fail if a dark CSS variable drifts from the TS token it's supposed to project.
        expect(readVar(`color-${group}-${shade}`, 0), `--color-${group}-${shade} (dark)`).toBe(
          hexToRgbTriplet(hex),
        );
      }
    }
    expect(readVar('canvas-bg', 0), '--canvas-bg (dark)').toBe(hexToRgbTriplet(canvas.dark));
  });

  it('light block matches `lightColors` exactly, shade for shade', () => {
    for (const [group, shades] of Object.entries(lightColors)) {
      for (const [shade, hex] of Object.entries(shades)) {
        // Must fail if a light CSS variable drifts from the TS token it's supposed to project.
        expect(readVar(`color-${group}-${shade}`, lightBlockStart), `--color-${group}-${shade} (light)`).toBe(
          hexToRgbTriplet(hex),
        );
      }
    }
    expect(readVar('canvas-bg', lightBlockStart), '--canvas-bg (light)').toBe(hexToRgbTriplet(canvas.light));
  });

  it('shadow variables match `shadows` (dark) / `lightShadows` (light)', () => {
    for (const key of ['sm', 'md', 'lg'] as const) {
      // Must fail if a shadow drifts from its TS token, in either theme — a dark-tuned shadow
      // reused as-is for light mode is the exact "heavy border/shadow" look this pair prevents.
      expect(readFreeformVar(`shadow-${key}`, 0), `--shadow-${key} (dark)`).toBe(shadows[key]);
      expect(readFreeformVar(`shadow-${key}`, lightBlockStart), `--shadow-${key} (light)`).toBe(lightShadows[key]);
    }
  });
});
