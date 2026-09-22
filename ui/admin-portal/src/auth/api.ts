// Login/logout calls against modules/auth. Kept separate from api/client.ts's request() helper:
// these have different semantics (credentials:'include' for the refresh cookie, no Authorization
// header needed, and a login failure must never trigger the refresh-retry loop that helper
// implements). Not under getBaseUrl()'s /v1 prefix — the auth router lives at /api/auth (see
// router.py's own header comment: it's the one route group not behind the shared service key).

import { ApiError, type ApiErrorCode, type AuthSession } from '../api/client';
import { getAccessToken, setAccessToken } from './tokenStore';

export interface LoginInput {
  email: string;
  password: string;
}

export async function login(input: LoginInput): Promise<AuthSession> {
  let res: Response;
  try {
    res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify(input),
    });
  } catch {
    throw new ApiError('INTERNAL_ERROR', 'Unable to reach the server.');
  }

  if (!res.ok) {
    const payload = await res.json().catch(() => undefined);
    const message =
      payload?.error?.message ?? (res.status === 401 ? 'Invalid email or password.' : 'Unable to sign in.');
    throw new ApiError(res.status === 401 ? 'UNAUTHORIZED' : 'INTERNAL_ERROR', message, res.status);
  }

  const session = (await res.json()) as AuthSession;
  setAccessToken(session.access_token);
  return session;
}

export interface ChangePasswordInput {
  current_password: string;
  new_password: string;
}

/**
 * POST /api/auth/password — needs the Bearer access token (get_authenticated_user reads it from
 * the Authorization header, not the refresh cookie), so this goes straight to tokenStore rather
 * than through api/client.ts's request() (which defaults to the /v1 prefix, not /api/auth).
 * Success revokes every refresh token for this account server-side (D5) — callers must always
 * follow a successful call with a full logout, never assume the current session still works.
 */
export async function changePassword(input: ChangePasswordInput): Promise<void> {
  let res: Response;
  try {
    res = await fetch('/api/auth/password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${getAccessToken() ?? ''}` },
      body: JSON.stringify(input),
    });
  } catch {
    throw new ApiError('INTERNAL_ERROR', 'Unable to reach the server.');
  }

  if (res.status === 204) return;

  const payload = await res.json().catch(() => undefined);
  if (!res.ok) {
    const message = payload?.error?.message ?? 'Unable to change password.';
    const code: ApiErrorCode =
      res.status === 401 ? 'UNAUTHORIZED' : res.status === 422 ? 'VALIDATION_ERROR' : 'INTERNAL_ERROR';
    throw new ApiError(code, message, res.status, payload?.error?.field);
  }
}

export async function logoutRequest(): Promise<void> {
  try {
    await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' });
  } catch {
    // Best-effort — client-side state is cleared regardless (see AuthProvider.logout).
  } finally {
    setAccessToken(null);
  }
}
