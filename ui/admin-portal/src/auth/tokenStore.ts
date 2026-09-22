/**
 * The live access token, held in memory only — never localStorage/sessionStorage (XSS-exposed)
 * and never a cookie the frontend reads itself. A plain module-level variable, same pattern as
 * MockAuthProvider's env-driven bridge: AuthProvider (a React tree) writes it, client.ts (a plain
 * module, can't call useAuth()) reads it on every request.
 *
 * Lost on full page reload by design — AuthProvider re-establishes it via a silent refresh()
 * call against the httpOnly refresh-token cookie on mount (see auth/api.ts).
 */
let accessToken: string | null = null;

export function setAccessToken(token: string | null): void {
  accessToken = token;
}

export function getAccessToken(): string | null {
  return accessToken;
}
