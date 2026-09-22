// Central typed HTTP client. Feature code calls the per-resource modules
// (users.ts, roles.ts, ...), never this file or raw fetch directly.
// No service/admin key is ever embedded here — see ADR-022, ADR-023 auth seam.

import { getCurrentOrgId } from '../auth/currentOrgId';
import { shouldUseRealAuth } from '../auth/env';
import { getAccessToken, setAccessToken } from '../auth/tokenStore';

export type ApiErrorCode =
  | 'VALIDATION_ERROR'
  | 'UNAUTHORIZED'
  | 'FORBIDDEN'
  | 'NOT_FOUND'
  | 'CONFLICT'
  | 'PROVISIONING_FAILED'
  | 'RATE_LIMITED'
  | 'INTERNAL_ERROR';

export class ApiError extends Error {
  constructor(
    public code: ApiErrorCode,
    message: string,
    public status?: number,
    public field?: string,
    public retryAfter?: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

const STATUS_TO_CODE: Record<number, ApiErrorCode> = {
  401: 'UNAUTHORIZED',
  403: 'FORBIDDEN',
  404: 'NOT_FOUND',
  409: 'CONFLICT',
  422: 'VALIDATION_ERROR',
  429: 'RATE_LIMITED',
};

export function getBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL || '/v1';
}

function codeForStatus(status: number): ApiErrorCode {
  if (status >= 500) return 'INTERNAL_ERROR';
  return STATUS_TO_CODE[status] ?? 'INTERNAL_ERROR';
}

/** Matches modules/auth/schemas.py's LoginResponse for organization-control-plane. `permissions`
 * is a list of permission UUIDs (the master catalog's ids), not slugs — AuthProvider resolves
 * them to slugs via listPermissions() before exposing AuthContextValue.permissions, the same
 * translation UserDetailPage already does for effective permissions. */
export interface AuthSession {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: { id: string; email: string; username: string; display_name: string | null };
  org: { id: string; name: string } | null;
  permissions: string[];
  must_change_password: boolean;
}

/**
 * POST /api/auth/refresh — no body, relies on the httpOnly `refresh` cookie the backend set on
 * login (path-scoped to /api/auth, `credentials: 'include'`, never read by JS). Returns a fresh
 * short-lived access token + session payload, or a 401 if the refresh cookie is missing, expired,
 * or revoked. Deduped: concurrent callers (e.g. several 401s at once) share one in-flight request.
 */
let refreshPromise: Promise<AuthSession | null> | null = null;

export async function refreshAccessToken(): Promise<AuthSession | null> {
  if (!refreshPromise) {
    refreshPromise = (async () => {
      try {
        const res = await fetch('/api/auth/refresh', { method: 'POST', credentials: 'include' });
        if (!res.ok) {
          setAccessToken(null);
          return null;
        }
        const session = (await res.json()) as AuthSession;
        setAccessToken(session.access_token);
        return session;
      } catch {
        setAccessToken(null);
        return null;
      } finally {
        refreshPromise = null;
      }
    })();
  }
  return refreshPromise;
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE';
  body?: unknown;
  /** Access token from the auth seam; a null/undefined token omits the header entirely. */
  accessToken?: string | null;
  /**
   * Mock-only (ADR-023): the mock backend checks this against the identity's permission set
   * independent of what the UI displayed (§74, §230) — proving backend enforcement, not a real
   * auth mechanism. Omit for calls that don't need to demonstrate this.
   */
  mockPermissions?: string[];
  signal?: AbortSignal;
}

function resolveToken(explicit: string | null | undefined): string | null {
  if (explicit !== undefined) return explicit;
  // Most callers never thread a token through (no page currently calls useAuth().getAccessToken()
  // directly — MockAuthProvider and the real AuthProvider both feed it through here instead).
  // Real auth: whatever the AuthProvider currently holds (see tokenStore.ts). Otherwise fall back
  // to the shared dev key so requests against a real backend (docker-compose.rbac.yml) still carry
  // one without a full login flow.
  return shouldUseRealAuth() ? getAccessToken() : import.meta.env.VITE_DEV_API_KEY || null;
}

async function doFetch(path: string, options: RequestOptions, token: string | null): Promise<Response> {
  const { method = 'GET', body, mockPermissions, signal } = options;
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (mockPermissions) headers['X-Mock-Permission'] = mockPermissions.join(',');
  // Every org-scoped route requires this (organization-control-plane's require_org_context).
  // MSW-mocked handlers ignore it; the real backend needs it on every request.
  const orgId = getCurrentOrgId();
  if (orgId) headers['X-Organization-Id'] = orgId;

  try {
    return await fetch(`${getBaseUrl()}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal,
    });
  } catch {
    // Transient failure: no HTTP response at all (offline, connection reset, aborted).
    throw new ApiError('INTERNAL_ERROR', 'Unable to reach the server.');
  }
}

export async function request<T>(path: string, options: RequestOptions = {}, _isRetry = false): Promise<T> {
  const token = resolveToken(options.accessToken);
  const response = await doFetch(path, options, token);

  // One silent-refresh-and-retry per request, only when the real AuthProvider (not
  // MockAuthProvider or the static VITE_DEV_API_KEY bridge) is active. Never retries a retry.
  if (response.status === 401 && !_isRetry && shouldUseRealAuth()) {
    const session = await refreshAccessToken();
    if (session) return request<T>(path, options, true);
  }

  if (response.status === 204) return undefined as T;

  const isJson = response.headers.get('content-type')?.includes('application/json');
  const payload = isJson ? await response.json().catch(() => undefined) : undefined;

  if (!response.ok) {
    const code = codeForStatus(response.status);
    // The real backend's envelope is `{ error: { message, type, field? } }` (ADR-003) — never a
    // flat `{ message, field }`. `field` is only ever present for a handful of known conflicts
    // (e.g. a uniqueness violation); most errors carry none.
    const errorBody =
      payload && typeof payload === 'object' && 'error' in payload && typeof payload.error === 'object'
        ? (payload.error as Record<string, unknown>)
        : undefined;
    const message =
      (errorBody && 'message' in errorBody && String(errorBody.message)) ||
      `Request failed with status ${response.status}`;
    const field = errorBody && 'field' in errorBody ? String(errorBody.field) : undefined;
    throw new ApiError(code, message, response.status, field);
  }

  return payload as T;
}
