import { useMutation, useQueryClient } from '@tanstack/react-query';
import { updateUser } from '../../../api/users';
import { queryKeys } from '../../../api/queryKeys';
import { useOrgId } from './useOrgId';

/** Reverses DeactivateUserDialog's status change. Not destructive, so unlike deactivate it's a
 * one-click row action — no confirmation dialog. Takes the target user id per call (rather than
 * a fixed id like useUpdateUser) since a single table renders one hook instance for every row. */
export function useReactivateUser() {
  const queryClient = useQueryClient();
  const orgId = useOrgId();

  return useMutation({
    mutationFn: (userId: string) => updateUser(userId, { status: 'active' }),
    onSuccess: (_user, userId) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.user(orgId, userId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.users(orgId) });
    },
  });
}
