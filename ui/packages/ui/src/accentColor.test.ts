import { afterEach, describe, expect, it } from 'vitest';
import { applyAccentColor, deriveAccentShades } from './accentColor';

const STYLE_ID = 'multistack-accent-override';

afterEach(() => {
  document.getElementById(STYLE_ID)?.remove();
});

describe('deriveAccentShades', () => {
  it('returns the base color unchanged as shade 500 in both modes', () => {
    // #3b82f6 -> "59 130 246", the same conversion globals.css itself uses.
    expect(deriveAccentShades('#3b82f6', 'dark')?.[500]).toBe('59 130 246');
    expect(deriveAccentShades('#3b82f6', 'light')?.[500]).toBe('59 130 246');
  });

  it('accepts 3-digit hex and a leading-# or bare form', () => {
    expect(deriveAccentShades('#fff', 'light')?.[500]).toBe(deriveAccentShades('#ffffff', 'light')?.[500]);
    expect(deriveAccentShades('7c3aed', 'light')?.[500]).toBe(deriveAccentShades('#7c3aed', 'light')?.[500]);
  });

  it('rejects invalid input rather than guessing', () => {
    expect(deriveAccentShades('not-a-color', 'dark')).toBeNull();
    expect(deriveAccentShades('#zzzzzz', 'dark')).toBeNull();
  });

  it('diverges 50/100/700 between dark and light for the same base color', () => {
    // The whole point of per-mode derivation (tokens/index.ts's documented dark/light
    // asymmetry) — must fail if dark and light collapse to the same near-black/near-white tints.
    const dark = deriveAccentShades('#7c3aed', 'dark')!;
    const light = deriveAccentShades('#7c3aed', 'light')!;
    expect(dark[50]).not.toBe(light[50]);
    expect(dark[100]).not.toBe(light[100]);
    expect(dark[700]).not.toBe(light[700]);
    // 500/600 track the shipped palette's own convention of staying identical across modes.
    expect(dark[500]).toBe(light[500]);
    expect(dark[600]).toBe(light[600]);
  });
});

describe('applyAccentColor', () => {
  it('injects a style tag overriding --color-primary-* for both root blocks', () => {
    applyAccentColor('#7c3aed');
    const style = document.getElementById(STYLE_ID);
    expect(style).not.toBeNull();
    expect(style!.textContent).toContain(":root {");
    expect(style!.textContent).toContain(":root[data-theme='light']");
    expect(style!.textContent).toContain('--color-primary-500:');
    expect(style!.textContent).toContain('--color-primary-700:');
  });

  it('is a no-op that clears any prior override when given undefined', () => {
    applyAccentColor('#7c3aed');
    expect(document.getElementById(STYLE_ID)).not.toBeNull();
    applyAccentColor(undefined);
    expect(document.getElementById(STYLE_ID)).toBeNull();
  });

  it('clears any prior override when given an invalid color, rather than leaving stale CSS', () => {
    applyAccentColor('#7c3aed');
    applyAccentColor('garbage');
    expect(document.getElementById(STYLE_ID)).toBeNull();
  });

  it('re-applying a new color updates the same style element rather than duplicating it', () => {
    applyAccentColor('#7c3aed');
    applyAccentColor('#059669');
    expect(document.querySelectorAll(`#${STYLE_ID}`).length).toBe(1);
    expect(document.getElementById(STYLE_ID)!.textContent).toContain('5 150 105'); // #059669
  });
});
