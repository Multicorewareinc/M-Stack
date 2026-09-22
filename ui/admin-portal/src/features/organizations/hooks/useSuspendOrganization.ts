import { useMutation, useQueryClient } from '@tanstack/react-query';
import { suspendOrganization } from '../../../api/organizations';
import { queryKeys } from '../../../api/queryKeys';

export function useSuspendOrganization(organizationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => suspendOrganization(organizationId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.organization(organizationId) });
      queryClient.invalidateQueries({ queryKey: ['organizations'] });
    },
  });
}
