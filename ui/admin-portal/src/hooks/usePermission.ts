import { useAuth } from '../auth/AuthContext';

/**
 * UX-only permission check (§73-76). Hides/disables controls for convenience;
 * the backend independently authorizes every request and is the only real
 * security boundary (§74, §230, §304).
 */
export function usePermission(slug: string): boolean {
  const { permissions } = useAuth();
  return permissions.includes(slug);
}
