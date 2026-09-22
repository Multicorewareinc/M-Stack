import { useQuery } from '@tanstack/react-query';
import { getUsageByKey } from '../../../api/usage';
import { queryKeys } from '../../../api/queryKeys';
import { useOrgId } from '../../users/hooks/useOrgId';

export function useUsageByKey() {
  const orgId = useOrgId();
  return useQuery({ queryKey: queryKeys.usageByKey(orgId), queryFn: () => getUsageByKey() });
}
