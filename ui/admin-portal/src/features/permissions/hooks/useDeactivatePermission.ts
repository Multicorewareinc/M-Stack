import { useMutation, useQueryClient } from '@tanstack/react-query';
import { deactivatePermission } from '../../../api/permissions';
import { queryKeys } from '../../../api/queryKeys';

export function useDeactivatePermission() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (permissionId: string) => deactivatePermission(permissionId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.permissions() }),
  });
}
