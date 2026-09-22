import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ApiError } from '../../../api/client';
import { activatePermission } from '../../../api/permissions';
import { queryKeys } from '../../../api/queryKeys';
import { useToast } from '../../../app/ToastProvider';

export function useActivatePermission() {
  const queryClient = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: (permissionId: string) => activatePermission(permissionId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.permissions() });
      toast.success('Permission reactivated');
    },
    onError: (err) => {
      toast.error('Unable to reactivate permission', err instanceof ApiError ? err.message : undefined);
    },
  });
}
