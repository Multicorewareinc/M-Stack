import { useMutation, useQueryClient } from '@tanstack/react-query';
import { updatePermission, type PermissionInput } from '../../../api/permissions';
import { queryKeys } from '../../../api/queryKeys';

export function useUpdatePermission(permissionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: Partial<PermissionInput>) => updatePermission(permissionId, input),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.permissions() }),
  });
}
