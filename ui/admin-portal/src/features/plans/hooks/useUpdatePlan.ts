import { useMutation, useQueryClient } from '@tanstack/react-query';
import { updatePlan, type PlanInput } from '../../../api/plans';
import { queryKeys } from '../../../api/queryKeys';

export function useUpdatePlan(planId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: Partial<PlanInput>) => updatePlan(planId, input),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.plans() }),
  });
}
