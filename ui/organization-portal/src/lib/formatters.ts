/**
 * Display formatters shared by this portal's feature pages.
 *
 * RECONSTRUCTED. The original never reached the repository: .gitignore
 * carried the standard Python `lib/` rule unanchored, so it matched
 * src/lib/ here too and this file was silently never committed. Five
 * pages import it, so `tsc --noEmit` failed in a clean clone with
 * TS2307 and neither portal could be built.
 *
 * The signatures are pinned by the call sites, so those are not a
 * guess. Two display choices are:
 *
 *   - the date format, fixed rather than locale-derived, so a table
 *     column is the same width for every viewer and a snapshot test
 *     does not depend on the runner's locale;
 *   - `formatQuota(0)` as "Unlimited", following the limiters' own
 *     convention -- rate-limiter-rpm's values.yaml reads "0 or absent
 *     means that scope is unlimited", and a plan row showing a bare 0
 *     would read as "no requests allowed", the opposite.
 *
 * Replace this with the original if it still exists; these two
 * decisions are the only thing worth checking.
 */

/** An ISO-8601 timestamp from the API, as "16 Sep 2026, 07:25". */
export function formatDate(value: string | null | undefined): string {
  if (!value) return '—';

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';

  const day = String(date.getDate()).padStart(2, '0');
  const month = MONTHS[date.getMonth()];
  const hours = String(date.getHours()).padStart(2, '0');
  const minutes = String(date.getMinutes()).padStart(2, '0');

  return `${day} ${month} ${date.getFullYear()}, ${hours}:${minutes}`;
}

/** A plain count, thousands-separated (e.g. "1,234,567") — same fixed locale as the rest of this
 * file, so the width is deterministic across viewers rather than locale-derived. */
export function formatNumber(value: number): string {
  return value.toLocaleString('en-GB');
}

/** A plan limit, as "Unlimited" or an abbreviated count ("100K", "1M") for a
 * round thousand/million — plan limits are round numbers in practice, and the
 * abbreviation is what the dashboard/settings/plans pages were built against.
 * Anything else (not a round thousand) falls back to a thousands-separated
 * count rather than guessing at a decimal abbreviation no call site expects. */
export function formatQuota(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  if (value === 0) return 'Unlimited';

  const abs = Math.abs(value);
  if (abs >= 1_000_000 && value % 1_000_000 === 0) return `${value / 1_000_000}M`;
  if (abs >= 1_000 && value % 1_000 === 0) return `${value / 1_000}K`;

  return value.toLocaleString('en-GB');
}

const MONTHS = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
];
