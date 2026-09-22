import { useMutation, useQueryClient } from '@tanstack/react-query';
import { updateUser, type UpdateUserInput } from '../../../api/users';
import { queryKeys } from '../../../api/queryKeys';
import { useOrgId } from './useOrgId';

export function useUpdateUser(userId: string) {
  const queryClient = useQueryClient();
  const orgId = useOrgId();

  return useMutation({
    mutationFn: (input: UpdateUserInput) => updateUser(userId, input),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.user(orgId, userId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.users(orgId) });
    },
  });
}
