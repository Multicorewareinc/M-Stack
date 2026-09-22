import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { getUserRoles, setUserRoles } from '../../../api/users';
import { queryKeys } from '../../../api/queryKeys';
import { useOrgId } from './useOrgId';

export function useUserRoles(userId: string) {
  const orgId = useOrgId();
  return useQuery({
    queryKey: queryKeys.userRoles(orgId, userId),
    queryFn: () => getUserRoles(userId),
    enabled: Boolean(userId),
  });
}

export function useSetUserRoles(userId: string) {
  const queryClient = useQueryClient();
  const orgId = useOrgId();

  return useMutation({
    mutationFn: (roleIds: string[]) => setUserRoles(userId, roleIds),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.userRoles(orgId, userId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.user(orgId, userId) });
    },
  });
}
