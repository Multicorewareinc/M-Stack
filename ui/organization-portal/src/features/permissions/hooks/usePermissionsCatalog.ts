import { useQuery } from '@tanstack/react-query';
import { listPermissions } from '../../../api/permissions';

/**
 * Platform-owned, not org-owned — shares the same query key as
 * RolePermissionsTab's catalog fetch (§70) so both consumers share one cache
 * entry rather than diverging. Longer staleTime since the catalog changes far
 * less often than user/role data.
 * TODO: confirm — no real invalidation-on-master-change event source exists
 * in this mock-first phase (§178-179); revisit once the real backend ships.
 */
export function usePermissionsCatalog() {
  return useQuery({
    queryKey: ['permissions-catalog'],
    queryFn: () => listPermissions(),
    staleTime: 5 * 60 * 1000,
  });
}
