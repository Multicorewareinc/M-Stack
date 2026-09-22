import { useMutation, useQueryClient } from '@tanstack/react-query';
import { deactivatePlan } from '../../../api/plans';
import { queryKeys } from '../../../api/queryKeys';

export function useDeactivatePlan() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (planId: string) => deactivatePlan(planId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.plans() }),
  });
}
