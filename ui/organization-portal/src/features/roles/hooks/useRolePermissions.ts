import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { getRolePermissions, setRolePermissions } from '../../../api/roles';
import { queryKeys } from '../../../api/queryKeys';
import { useOrgId } from '../../users/hooks/useOrgId';

export function useRolePermissions(roleId: string) {
  const orgId = useOrgId();
  return useQuery({
    queryKey: queryKeys.rolePermissions(orgId, roleId),
    queryFn: () => getRolePermissions(roleId),
    enabled: Boolean(roleId),
  });
}

/** Persists exactly the selection it's given — bulk-select UI conveniences never alter the submitted payload (§68). */
export function useSetRolePermissions(roleId: string) {
  const queryClient = useQueryClient();
  const orgId = useOrgId();
  return useMutation({
    mutationFn: (permissionIds: string[]) => setRolePermissions(roleId, permissionIds),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.rolePermissions(orgId, roleId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.roles(orgId) });
    },
  });
}
