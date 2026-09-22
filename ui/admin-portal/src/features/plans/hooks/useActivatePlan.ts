import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ApiError } from '../../../api/client';
import { activatePlan } from '../../../api/plans';
import { queryKeys } from '../../../api/queryKeys';
import { useToast } from '../../../app/ToastProvider';

export function useActivatePlan() {
  const queryClient = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: (planId: string) => activatePlan(planId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.plans() });
      toast.success('Plan reactivated');
    },
    onError: (err) => {
      toast.error('Unable to reactivate plan', err instanceof ApiError ? err.message : undefined);
    },
  });
}
