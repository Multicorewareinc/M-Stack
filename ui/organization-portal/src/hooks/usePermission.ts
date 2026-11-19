import { useAuth } from '../auth/AuthContext';

/**
 * UX-only permission check (§73-76). Hides/disables controls for convenience;
 * the backend independently authorizes every request and is the only real
 * security boundary (§74, §230, §304).
 *
 * `slug` may be a single permission or an array — an array matches if the user holds ANY of them
 * (logical OR), for sections gated by more than one granular permission (e.g. API Keys, which
 * composes apikey.create/update/delete).
 */
export function usePermission(slug: string | string[]): boolean {
  const { permissions } = useAuth();
  const slugs = Array.isArray(slug) ? slug : [slug];
  return slugs.some((s) => permissions.includes(s));
}
