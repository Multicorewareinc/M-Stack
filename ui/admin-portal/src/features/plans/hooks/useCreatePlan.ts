import { useMutation, useQueryClient } from '@tanstack/react-query';
import { createPlan, type PlanInput } from '../../../api/plans';
import { queryKeys } from '../../../api/queryKeys';

export function useCreatePlan() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: PlanInput) => createPlan(input),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.plans() }),
  });
}
