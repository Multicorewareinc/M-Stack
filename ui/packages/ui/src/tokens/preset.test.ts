import { describe, expect, it } from 'vitest';
import { colors, spacing, radius } from './index';
import { multistackPreset } from './preset';

describe('tailwind preset', () => {
  it('preset maps token values', () => {
    const extend = multistackPreset.theme?.extend as Record<string, unknown>;
    // Must fail if the preset hard-codes values instead of referencing tokens.
    expect(extend.colors).toMatchObject({ primary: colors.primary, neutral: colors.neutral });
    expect(extend.spacing).toBe(spacing);
    expect(extend.borderRadius).toBe(radius);
  });

  it('projects a known spacing value', () => {
    const extend = multistackPreset.theme?.extend as { spacing: Record<string, string> };
    expect(extend.spacing['4']).toBe('16px');
  });
});
