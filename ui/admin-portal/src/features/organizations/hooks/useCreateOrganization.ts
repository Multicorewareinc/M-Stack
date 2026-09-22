import { useMutation, useQueryClient } from '@tanstack/react-query';
import { createOrganization, type CreateOrganizationInput } from '../../../api/organizations';

export function useCreateOrganization() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: CreateOrganizationInput) => createOrganization(input),
    onSuccess: () => {
      // Matches every search-scoped organizations query (§94 invalidation graph).
      queryClient.invalidateQueries({ queryKey: ['organizations'] });
    },
  });
}
