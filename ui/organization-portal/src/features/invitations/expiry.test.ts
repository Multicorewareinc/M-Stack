import { describe, expect, it } from 'vitest';
import { formatExpiry, isExpired } from './expiry';

const NOW = new Date(2026, 5, 10, 12, 0, 0); // local time, so the printed date is stable in any time zone
const at = (ms: number) => new Date(NOW.getTime() + ms).toISOString();
const HOUR = 60 * 60 * 1000;
const DAY = 24 * HOUR;

describe('formatExpiry', () => {
  it('counts whole days ahead, with the date', () => {
    expect(formatExpiry(at(3 * DAY + 2 * HOUR), NOW)).toBe('in 3d · 13 Jun 2026');
    expect(formatExpiry(at(DAY), NOW)).toBe('in 1d · 11 Jun 2026');
  });

  it('switches to hours inside the last day, never showing 0h', () => {
    expect(formatExpiry(at(5 * HOUR), NOW)).toBe('in 5h · 10 Jun 2026');
    expect(formatExpiry(at(10 * 60 * 1000), NOW)).toBe('in 1h · 10 Jun 2026');
  });

  it('says Expired once the moment has passed', () => {
    expect(formatExpiry(at(-1), NOW)).toBe('Expired · 10 Jun 2026');
    expect(formatExpiry(at(0), NOW)).toBe('Expired · 10 Jun 2026');
    expect(isExpired(at(-1), NOW)).toBe(true);
    expect(isExpired(at(HOUR), NOW)).toBe(false);
  });

  it('shows a dash for a missing or invalid value', () => {
    expect(formatExpiry(null, NOW)).toBe('—');
    expect(formatExpiry('not a date', NOW)).toBe('—');
    expect(isExpired(undefined, NOW)).toBe(false);
  });
});
