import { useQuery } from '@tanstack/react-query';
import { getRole } from '../../../api/roles';
import { queryKeys } from '../../../api/queryKeys';
import { useOrgId } from '../../users/hooks/useOrgId';

export function useRole(roleId: string) {
  const orgId = useOrgId();
  return useQuery({
    queryKey: queryKeys.role(orgId, roleId),
    queryFn: () => getRole(roleId),
    enabled: Boolean(roleId),
    retry: false,
  });
}
