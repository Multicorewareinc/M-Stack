import { useMutation, useQueryClient } from '@tanstack/react-query';
import { deleteUser } from '../../../api/users';
import { queryKeys } from '../../../api/queryKeys';
import { useOrgId } from './useOrgId';

export function useDeleteUser() {
  const queryClient = useQueryClient();
  const orgId = useOrgId();
  return useMutation({
    mutationFn: (userId: string) => deleteUser(userId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.users(orgId) }),
  });
}
