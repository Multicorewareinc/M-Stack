import { useQuery } from '@tanstack/react-query';
import { listApiKeys } from '../../../api/apiKeys';
import { queryKeys } from '../../../api/queryKeys';
import { useOrgId } from '../../users/hooks/useOrgId';

export function useApiKeys() {
  const orgId = useOrgId();
  return useQuery({ queryKey: queryKeys.apiKeys(orgId), queryFn: () => listApiKeys() });
}
