import { useMutation, useQueryClient } from '@tanstack/react-query';
import { revokeApiKey } from '../../../api/apiKeys';
import { queryKeys } from '../../../api/queryKeys';
import { useOrgId } from '../../users/hooks/useOrgId';

export function useRevokeApiKey() {
  const queryClient = useQueryClient();
  const orgId = useOrgId();
  return useMutation({
    mutationFn: (id: string) => revokeApiKey(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.apiKeys(orgId) }),
  });
}
