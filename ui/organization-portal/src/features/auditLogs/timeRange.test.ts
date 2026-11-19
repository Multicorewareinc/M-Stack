import { describe, expect, it } from 'vitest';
import { isCustomRangeInvalid, resolveTimeBounds } from './timeRange';

const NOW = new Date('2026-06-10T12:00:00Z');

describe('resolveTimeBounds', () => {
  it('applies no bounds for "all time"', () => {
    expect(resolveTimeBounds('all', '', '', NOW)).toEqual({});
  });

  it('measures rolling presets back from now and leaves the upper end open', () => {
    expect(resolveTimeBounds('1h', '', '', NOW)).toEqual({ since: '2026-06-10T11:00:00.000Z' });
    expect(resolveTimeBounds('24h', '', '', NOW)).toEqual({ since: '2026-06-09T12:00:00.000Z' });
    expect(resolveTimeBounds('7d', '', '', NOW)).toEqual({ since: '2026-06-03T12:00:00.000Z' });
    expect(resolveTimeBounds('30d', '', '', NOW)).toEqual({ since: '2026-05-11T12:00:00.000Z' });
  });

  it('treats custom dates as whole local days and allows an open end', () => {
    const b = resolveTimeBounds('custom', '2026-06-01', '2026-06-02', NOW);
    expect(b.since).toBe(new Date('2026-06-01T00:00:00').toISOString());
    expect(b.until).toBe(new Date('2026-06-02T23:59:59.999').toISOString());

    expect(resolveTimeBounds('custom', '2026-06-01', '', NOW)).toEqual({
      since: new Date('2026-06-01T00:00:00').toISOString(),
      until: undefined,
    });
    expect(resolveTimeBounds('custom', '', '', NOW)).toEqual({ since: undefined, until: undefined });
  });

  it('applies no bounds for an inverted custom range', () => {
    expect(isCustomRangeInvalid('2026-06-05', '2026-06-01')).toBe(true);
    expect(isCustomRangeInvalid('2026-06-01', '2026-06-01')).toBe(false);
    expect(isCustomRangeInvalid('2026-06-01', '')).toBe(false);
    expect(resolveTimeBounds('custom', '2026-06-05', '2026-06-01', NOW)).toEqual({});
  });
});
