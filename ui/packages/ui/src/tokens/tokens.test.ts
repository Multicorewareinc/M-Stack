import { describe, expect, it } from 'vitest';
import { tokens } from './index';

describe('design tokens', () => {
  it('exposes all token groups', () => {
    // Must fail if a group is missing.
    for (const group of [
      'colors',
      'typography',
      'spacing',
      'radius',
      'borders',
      'shadows',
      'zIndex',
      'breakpoints',
      'motion',
    ] as const) {
      expect(tokens[group], `token group ${group}`).toBeDefined();
    }
  });

  it('exposes concrete, typed values', () => {
    expect(tokens.spacing[4]).toBe('16px');
    expect(tokens.radius.md).toBe('6px');
    expect(typeof tokens.zIndex.modal).toBe('number');
    expect(tokens.colors.primary[600]).toMatch(/^#[0-9a-f]{6}$/i);
    expect(tokens.breakpoints['2xl']).toBe('1440px');
  });
});
