// API keys — modules/api_keys on organization-control-plane. Lives at /api/api-keys, NOT under
// getBaseUrl()'s /v1 prefix (mirrors auth/api.ts's own note: it's an end-user JWT surface, like
// /api/auth, gated by get_current_user — a real signed-in identity, never the shared
// SERVICE_API_KEY/VITE_DEV_API_KEY bridge). So every call here goes straight to fetch() with the
// Bearer token from tokenStore, not through api/client.ts's request(). Mutations additionally
// require the `apikey.manage` permission server-side (403 if the caller's role doesn't grant it).
import { ApiError, type ApiErrorCode } from './client';
import { getAccessToken } from '../auth/tokenStore';

const BASE = '/api/api-keys';

export type ApiKeyStatus = 'active' | 'expired' | 'revoked';

export interface ApiKey {
  id: string;
  org_id: string;
  owner_id: string;
  name: string;
  prefix: string;
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  status: ApiKeyStatus;
}

export interface ApiKeyCreated extends ApiKey {
  /** The plaintext secret — present ONLY in the create/rotate response, never again. */
  raw_key: string;
}

export interface ApiKeyCreateInput {
  name: string;
  expires_at?: string | null;
}

export interface ApiKeyUpdateInput {
  name?: string;
  expires_at?: string | null;
}

async function authedRequest<T>(path: string, options: { method?: string; body?: unknown } = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      method: options.method ?? 'GET',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${getAccessToken() ?? ''}` },
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    });
  } catch {
    throw new ApiError('INTERNAL_ERROR', 'Unable to reach the server.');
  }

  if (res.status === 204) return undefined as T;

  const isJson = res.headers.get('content-type')?.includes('application/json');
  const payload = isJson ? await res.json().catch(() => undefined) : undefined;

  if (!res.ok) {
    const message = payload?.error?.message ?? 'Request failed.';
    const code: ApiErrorCode =
      res.status === 401
        ? 'UNAUTHORIZED'
        : res.status === 403
          ? 'FORBIDDEN'
          : res.status === 404
            ? 'NOT_FOUND'
            : res.status === 409
              ? 'CONFLICT'
              : res.status === 422
                ? 'VALIDATION_ERROR'
                : 'INTERNAL_ERROR';
    throw new ApiError(code, message, res.status, payload?.error?.field);
  }

  return payload as T;
}

export function listApiKeys(): Promise<ApiKey[]> {
  return authedRequest<ApiKey[]>('');
}

export function createApiKey(input: ApiKeyCreateInput): Promise<ApiKeyCreated> {
  return authedRequest<ApiKeyCreated>('', { method: 'POST', body: input });
}

export function rotateApiKey(id: string): Promise<ApiKeyCreated> {
  return authedRequest<ApiKeyCreated>(`/${id}/rotate`, { method: 'POST' });
}

export function updateApiKey(id: string, input: ApiKeyUpdateInput): Promise<ApiKey> {
  return authedRequest<ApiKey>(`/${id}`, { method: 'PATCH', body: input });
}

/** DELETE here is a revoke, not a hard delete — the backend has no separate hard-delete route
 * (revoked keys stay listed, same as the roles/plans "soft action only" pattern elsewhere). */
export function revokeApiKey(id: string): Promise<void> {
  return authedRequest<void>(`/${id}`, { method: 'DELETE' });
}
