import { useQuery } from '@tanstack/react-query';
import { listRoles } from '../../../api/roles';
import { queryKeys } from '../../../api/queryKeys';
import { useOrgId } from './useOrgId';

/** Read-only roles lookup for filters/pickers here; full Roles CRUD is SP-09. */
export function useRolesForSelect() {
  const orgId = useOrgId();
  return useQuery({ queryKey: queryKeys.roles(orgId), queryFn: () => listRoles() });
}
