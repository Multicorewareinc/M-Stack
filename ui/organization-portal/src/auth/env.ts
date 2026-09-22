/**
 * Whether the temporary mock auth/MSW harness should be active. True in dev
 * and test; false in a production build. There is no real auth provider yet
 * (Architecture §44) — main.tsx logs a warning and still falls back to the
 * mock rather than silently shipping it unlabeled once a real provider exists
 * to swap in (ADR-023).
 */
export function shouldUseMockAuth(): boolean {
  return import.meta.env.DEV || import.meta.env.MODE === 'test';
}

/**
 * Whether App.tsx mounts the real AuthProvider (login/refresh flow against /api/auth, backed by
 * the modules/auth JWT + refresh-cookie implementation) instead of MockAuthProvider. Always true
 * in a production build; in dev, opt in with VITE_REAL_AUTH=true (independent of VITE_DEV_API_KEY,
 * the older static-shared-key bridge — don't set both).
 */
export function shouldUseRealAuth(): boolean {
  return import.meta.env.PROD || import.meta.env.VITE_REAL_AUTH === 'true';
}
