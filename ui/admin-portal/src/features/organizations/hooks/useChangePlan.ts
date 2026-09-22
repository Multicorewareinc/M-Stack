import { useMutation, useQueryClient } from '@tanstack/react-query';
import { changeOrganizationPlan } from '../../../api/organizations';
import { queryKeys } from '../../../api/queryKeys';

export function useChangePlan(organizationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (planId: string) => changeOrganizationPlan(organizationId, planId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.organization(organizationId) });
      queryClient.invalidateQueries({ queryKey: ['organizations'] });
    },
  });
}
