import { useQuery } from '@tanstack/react-query';
import { getUsageByUser } from '../../../api/usage';
import { queryKeys } from '../../../api/queryKeys';
import { useOrgId } from '../../users/hooks/useOrgId';

export function useUsageByUser() {
  const orgId = useOrgId();
  return useQuery({ queryKey: queryKeys.usageByUser(orgId), queryFn: () => getUsageByUser() });
}
