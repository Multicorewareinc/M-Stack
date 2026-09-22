import { useMutation, useQueryClient } from '@tanstack/react-query';
import { updateApiKey, type ApiKeyUpdateInput } from '../../../api/apiKeys';
import { queryKeys } from '../../../api/queryKeys';
import { useOrgId } from '../../users/hooks/useOrgId';

export function useUpdateApiKey(id: string) {
  const queryClient = useQueryClient();
  const orgId = useOrgId();
  return useMutation({
    mutationFn: (input: ApiKeyUpdateInput) => updateApiKey(id, input),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.apiKeys(orgId) }),
  });
}
