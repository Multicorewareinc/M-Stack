import { useMutation, useQueryClient } from '@tanstack/react-query';
import { createMasterPermission, type PermissionInput } from '../../../api/permissions';
import { queryKeys } from '../../../api/queryKeys';
import { usePermission } from '../../../hooks/usePermission';

export function useCreatePermission() {
  const queryClient = useQueryClient();
  const canCreate = usePermission('permissions.create');

  return useMutation({
    mutationFn: (input: PermissionInput) => createMasterPermission(input, canCreate ? ['permissions.create'] : []),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.permissions() }),
  });
}
