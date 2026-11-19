const DAY_MS = 24 * 60 * 60 * 1000;
const HOUR_MS = 60 * 60 * 1000;

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function shortDate(date: Date): string {
  return `${date.getDate()} ${MONTHS[date.getMonth()]} ${date.getFullYear()}`;
}

/** When an invitation lapses, as "in 3d · 26 Sep 2026" ("in 5h" inside the last day), or
 * "Expired · 26 Sep 2026" once it has. Fixed format (not locale-derived) like the portal's other dates. */
export function formatExpiry(value: string | null | undefined, now: Date = new Date()): string {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return '—';
  const remaining = date.getTime() - now.getTime();
  if (remaining <= 0) return `Expired · ${shortDate(date)}`;
  const relative = remaining >= DAY_MS ? `in ${Math.floor(remaining / DAY_MS)}d` : `in ${Math.max(1, Math.floor(remaining / HOUR_MS))}h`;
  return `${relative} · ${shortDate(date)}`;
}

export function isExpired(value: string | null | undefined, now: Date = new Date()): boolean {
  const date = value ? new Date(value) : null;
  return Boolean(date && !Number.isNaN(date.getTime()) && date.getTime() <= now.getTime());
}
