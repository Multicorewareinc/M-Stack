import { useMutation, useQueryClient } from '@tanstack/react-query';
import { createRole, type RoleInput } from '../../../api/roles';
import { queryKeys } from '../../../api/queryKeys';
import { useOrgId } from '../../users/hooks/useOrgId';

export function useCreateRole() {
  const queryClient = useQueryClient();
  const orgId = useOrgId();
  return useMutation({
    mutationFn: (input: RoleInput) => createRole(input),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.roles(orgId) }),
  });
}
