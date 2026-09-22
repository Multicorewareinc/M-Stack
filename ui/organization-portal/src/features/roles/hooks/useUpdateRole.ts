import { useMutation, useQueryClient } from '@tanstack/react-query';
import { updateRole, type RoleInput } from '../../../api/roles';
import { queryKeys } from '../../../api/queryKeys';
import { useOrgId } from '../../users/hooks/useOrgId';

export function useUpdateRole(roleId: string) {
  const queryClient = useQueryClient();
  const orgId = useOrgId();
  return useMutation({
    mutationFn: (input: Partial<RoleInput>) => updateRole(roleId, input),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.role(orgId, roleId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.roles(orgId) });
    },
  });
}
