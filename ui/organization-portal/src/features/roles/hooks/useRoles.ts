import { useQuery } from '@tanstack/react-query';
import { listRoles } from '../../../api/roles';
import { queryKeys } from '../../../api/queryKeys';
import { useOrgId } from '../../users/hooks/useOrgId';

export function useRoles() {
  const orgId = useOrgId();
  return useQuery({ queryKey: queryKeys.roles(orgId), queryFn: () => listRoles() });
}
