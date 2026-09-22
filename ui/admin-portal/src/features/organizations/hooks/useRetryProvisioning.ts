import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ApiError } from '../../../api/client';
import { retryProvisioning } from '../../../api/organizations';
import { queryKeys } from '../../../api/queryKeys';
import { useToast } from '../../../app/ToastProvider';

export function useRetryProvisioning() {
  const queryClient = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: (organizationId: string) => retryProvisioning(organizationId),
    onSuccess: (_data, organizationId) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.organization(organizationId) });
      queryClient.invalidateQueries({ queryKey: ['organizations'] });
      toast.success('Provisioning retried');
    },
    onError: (err) => {
      toast.error('Unable to retry provisioning', err instanceof ApiError ? err.message : undefined);
    },
  });
}
