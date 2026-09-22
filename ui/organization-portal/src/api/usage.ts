import { request } from './client';

// Org CP's /v1/usage/* (add-org-cp-usage-proxy) — a thin read-through proxy to billing's internal
// usage-reporting surface, org-scoped like every other /v1/* route (X-Organization-Id / JWT `org`
// claim, attached automatically by request()). No cost/price field — the underlying ledger has
// token counts, never a price.

export interface UsagePeriod {
  start: string;
  end: string;
}

export interface UsageSummary {
  requests: number;
  total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
  period: UsagePeriod;
}

export interface UsageBucket {
  date: string;
  requests: number;
  total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
}

export interface UsageTimeseries {
  period: UsagePeriod;
  buckets: UsageBucket[];
}

/** `start`/`end` are both-or-neither (a lone one is ignored server-side); omit both for the
 * current UTC calendar month. */
function periodQuery(start?: string, end?: string): string {
  return start && end ? `?start=${start}&end=${end}` : '';
}

export function getUsageSummary(start?: string, end?: string): Promise<UsageSummary> {
  return request<UsageSummary>(`/usage/summary${periodQuery(start, end)}`);
}

export function getUsageTimeseries(start?: string, end?: string): Promise<UsageTimeseries> {
  return request<UsageTimeseries>(`/usage/timeseries${periodQuery(start, end)}`);
}

// Per-user / per-API-key attribution (add-per-user-key-usage-attribution). `owner_id`/
// `api_key_id` is null for one bucket per response at most — usage the enricher couldn't
// attribute (e.g. events predating this pipeline) — never omitted, so the per-key/per-user rows
// always reconcile with /usage/summary's org-wide totals.

export interface UsageByUserRow {
  owner_id: string | null;
  requests: number;
  total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
}

export interface UsageByUser {
  period: UsagePeriod;
  users: UsageByUserRow[];
}

export interface UsageByKeyRow {
  api_key_id: string | null;
  requests: number;
  total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
}

export interface UsageByKey {
  period: UsagePeriod;
  api_keys: UsageByKeyRow[];
}

export function getUsageByUser(start?: string, end?: string): Promise<UsageByUser> {
  return request<UsageByUser>(`/usage/by-user${periodQuery(start, end)}`);
}

export function getUsageByKey(start?: string, end?: string): Promise<UsageByKey> {
  return request<UsageByKey>(`/usage/by-key${periodQuery(start, end)}`);
}
