import { request } from './client';
import type { AuditLogPage } from './types';

export interface AuditLogFilters {
  organizationId?: string;
  action?: string;
  /** ISO-8601; only events at or after this instant. */
  since?: string;
  /** ISO-8601; only events at or before this instant. */
  until?: string;
  search?: string;
}

export function listAuditLogs(
  filters: AuditLogFilters,
  limit: number,
  offset: number,
  accessToken?: string | null,
): Promise<AuditLogPage> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (filters.organizationId) params.set('organization_id', filters.organizationId);
  if (filters.action) params.set('action', filters.action);
  if (filters.since) params.set('since', filters.since);
  if (filters.until) params.set('until', filters.until);
  if (filters.search) params.set('search', filters.search);
  return request<AuditLogPage>(`/audit-logs?${params.toString()}`, { accessToken });
}
