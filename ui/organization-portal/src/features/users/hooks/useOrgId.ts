import { useAuth } from '../../../auth/AuthContext';

/** Canonical way to read the authenticated organization id (§77-78) — never a client-supplied value. */
export function useOrgId(): string {
  return useAuth().organizationContext.id;
}
