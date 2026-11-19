/** The "Time range" filter: a few rolling presets plus a custom from/to date. */
export const TIME_RANGES = [
  { value: 'all', label: 'All time' },
  { value: '1h', label: 'Last hour' },
  { value: '24h', label: 'Last 24 hours' },
  { value: '7d', label: 'Last 7 days' },
  { value: '30d', label: 'Last 30 days' },
  { value: 'custom', label: 'Custom range' },
] as const;

export type TimeRangePreset = (typeof TIME_RANGES)[number]['value'];

const PRESET_MS: Record<string, number> = {
  '1h': 60 * 60 * 1000,
  '24h': 24 * 60 * 60 * 1000,
  '7d': 7 * 24 * 60 * 60 * 1000,
  '30d': 30 * 24 * 60 * 60 * 1000,
};

export interface TimeBounds {
  /** ISO-8601, inclusive lower bound. */
  since?: string;
  /** ISO-8601, inclusive upper bound. */
  until?: string;
}

/** A custom range is invalid when both dates are set and "from" falls after "to". */
export function isCustomRangeInvalid(from: string, to: string): boolean {
  return Boolean(from && to && from > to);
}

/**
 * Turns the chosen range into the API's `since` / `until`. Rolling presets are measured back from
 * `now`; custom dates are whole local days (00:00:00 of "from" through 23:59:59.999 of "to"), and
 * either end may be left open. An invalid custom range applies no bounds rather than an empty one.
 */
export function resolveTimeBounds(preset: TimeRangePreset, from: string, to: string, now: Date): TimeBounds {
  if (preset === 'custom') {
    if (isCustomRangeInvalid(from, to)) return {};
    return {
      since: from ? new Date(`${from}T00:00:00`).toISOString() : undefined,
      until: to ? new Date(`${to}T23:59:59.999`).toISOString() : undefined,
    };
  }
  const ms = PRESET_MS[preset];
  return ms ? { since: new Date(now.getTime() - ms).toISOString() } : {};
}
