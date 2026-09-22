import { useMutation, useQueryClient } from '@tanstack/react-query';
import { rotateApiKey } from '../../../api/apiKeys';
import { queryKeys } from '../../../api/queryKeys';
import { useOrgId } from '../../users/hooks/useOrgId';

export function useRotateApiKey() {
  const queryClient = useQueryClient();
  const orgId = useOrgId();
  return useMutation({
    mutationFn: (id: string) => rotateApiKey(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.apiKeys(orgId) }),
  });
}
