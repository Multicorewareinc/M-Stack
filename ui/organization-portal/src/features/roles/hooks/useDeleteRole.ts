import { useMutation, useQueryClient } from '@tanstack/react-query';
import { deleteRole } from '../../../api/roles';
import { queryKeys } from '../../../api/queryKeys';
import { useOrgId } from '../../users/hooks/useOrgId';

export function useDeleteRole() {
  const queryClient = useQueryClient();
  const orgId = useOrgId();
  return useMutation({
    mutationFn: (roleId: string) => deleteRole(roleId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.roles(orgId) }),
  });
}
