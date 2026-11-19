// Audit logs — modules/audit on organization-control-plane. Lives at /api/audit-logs, NOT under
// getBaseUrl()'s /v1 prefix: like /api/api-keys it is an end-user JWT surface, and the organization
// is taken from the verified token server-side (never from a client value), so a signed-in user
// can only ever read their own organization's trail. Hence a straight fetch() with the Bearer
// token from tokenStore rather than api/client.ts's request().
import { ApiError, type ApiErrorCode } from './client';
import { getAccessToken } from '../auth/tokenStore';

const BASE = '/api/audit-logs';

export interface AuditLog {
  id: string;
  organization_id: string | null;
  /** 'user' (org member) | 'admin' (platform super-admin acting for the org) | 'service' | 'anonymous'. */
  actor_type: string;
  actor_id: string | null;
  actor_email: string | null;
  /** Dotted event name, e.g. 'api_key.created'. */
  action: string;
  resource_type: string;
  resource_id: string | null;
  resource_name: string | null;
  details: Record<string, unknown>;
  ip_address: string | null;
  created_at: string;
}

export interface AuditLogPage {
  items: AuditLog[];
  total: number;
  limit: number;
  offset: number;
}

export interface AuditLogFilters {
  action?: string;
  /** ISO-8601; only events at or after this instant. */
  since?: string;
  /** ISO-8601; only events at or before this instant. */
  until?: string;
  search?: string;
}

const STATUS_TO_CODE: Record<number, ApiErrorCode> = {
  401: 'UNAUTHORIZED',
  403: 'FORBIDDEN',
  404: 'NOT_FOUND',
  422: 'VALIDATION_ERROR',
};

export async function listAuditLogs(
  filters: AuditLogFilters,
  limit: number,
  offset: number,
): Promise<AuditLogPage> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (filters.action) params.set('action', filters.action);
  if (filters.since) params.set('since', filters.since);
  if (filters.until) params.set('until', filters.until);
  if (filters.search) params.set('search', filters.search);

  let res: Response;
  try {
    res = await fetch(`${BASE}?${params.toString()}`, {
      headers: { Authorization: `Bearer ${getAccessToken() ?? ''}` },
    });
  } catch {
    throw new ApiError('INTERNAL_ERROR', 'Unable to reach the server.');
  }

  const isJson = res.headers.get('content-type')?.includes('application/json');
  const payload = isJson ? await res.json().catch(() => undefined) : undefined;
  if (!res.ok) {
    throw new ApiError(
      STATUS_TO_CODE[res.status] ?? 'INTERNAL_ERROR',
      payload?.error?.message ?? 'Request failed.',
      res.status,
    );
  }
  return payload as AuditLogPage;
}
