import { useMutation, useQueryClient } from '@tanstack/react-query';
import { deleteOrganization } from '../../../api/organizations';

export function useDeleteOrganization() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (organizationId: string) => deleteOrganization(organizationId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['organizations'] }),
  });
}
