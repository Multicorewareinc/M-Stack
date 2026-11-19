import { describe, expect, it } from 'vitest';
import { colors, spacing, radius } from './index';
import { multistackPreset } from './preset';

describe('tailwind preset', () => {
  it('preset maps color shapes to CSS-variable references, not literal hex', () => {
    const extend = multistackPreset.theme?.extend as {
      colors: Record<string, Record<string, string>>;
      spacing: unknown;
      borderRadius: unknown;
    };
    // Must fail if a group's shade keys drift from the token source (dark `colors` supplies the
    // shape; the light theme swap depends on both palettes sharing identical keys).
    for (const group of ['neutral', 'primary', 'success', 'warning', 'danger', 'info'] as const) {
      expect(Object.keys(extend.colors[group]).sort()).toEqual(Object.keys(colors[group]).sort());
      // Must fail if the preset hard-codes a literal hex instead of a CSS-variable reference —
      // that's what makes the [data-theme] runtime swap work without a rebuild.
      for (const [shade, value] of Object.entries(extend.colors[group])) {
        expect(value).toBe(`rgb(var(--color-${group}-${shade}) / <alpha-value>)`);
      }
    }
    expect(extend.spacing).toBe(spacing);
    expect(extend.borderRadius).toBe(radius);
  });

  it('projects a known spacing value', () => {
    const extend = multistackPreset.theme?.extend as { spacing: Record<string, string> };
    expect(extend.spacing['4']).toBe('16px');
  });

  it('maps border-subtle/default/strong to the same neutral CSS variables as the color scale', () => {
    const extend = multistackPreset.theme?.extend as { borderColor: Record<string, string> };
    // Must fail if border-* is reintroduced as a literal hex — that's the bug that left borders
    // frozen to the dark palette regardless of the [data-theme] toggle.
    expect(extend.borderColor.subtle).toBe('rgb(var(--color-neutral-200) / <alpha-value>)');
    expect(extend.borderColor.default).toBe('rgb(var(--color-neutral-300) / <alpha-value>)');
    expect(extend.borderColor.strong).toBe('rgb(var(--color-neutral-400) / <alpha-value>)');
  });

  it('maps shadow-sm/md/lg to CSS variables, not literal box-shadow strings', () => {
    const extend = multistackPreset.theme?.extend as { boxShadow: Record<string, string> };
    // Must fail if a shadow is reintroduced as a literal — dark-tuned shadow alpha reads as a
    // heavy smudge on a light background, so this needs the same per-theme swap colors get.
    expect(extend.boxShadow.none).toBe('none');
    expect(extend.boxShadow.sm).toBe('var(--shadow-sm)');
    expect(extend.boxShadow.md).toBe('var(--shadow-md)');
    expect(extend.boxShadow.lg).toBe('var(--shadow-lg)');
  });
});
