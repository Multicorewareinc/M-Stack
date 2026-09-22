import { useMutation, useQueryClient } from '@tanstack/react-query';
import { createApiKey, type ApiKeyCreateInput } from '../../../api/apiKeys';
import { queryKeys } from '../../../api/queryKeys';
import { useOrgId } from '../../users/hooks/useOrgId';

export function useCreateApiKey() {
  const queryClient = useQueryClient();
  const orgId = useOrgId();
  return useMutation({
    mutationFn: (input: ApiKeyCreateInput) => createApiKey(input),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.apiKeys(orgId) }),
  });
}
