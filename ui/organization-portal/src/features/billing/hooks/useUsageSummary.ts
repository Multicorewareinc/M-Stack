import { useQuery } from '@tanstack/react-query';
import { getUsageSummary } from '../../../api/usage';
import { queryKeys } from '../../../api/queryKeys';
import { useOrgId } from '../../users/hooks/useOrgId';

export function useUsageSummary() {
  const orgId = useOrgId();
  return useQuery({ queryKey: queryKeys.usageSummary(orgId), queryFn: () => getUsageSummary() });
}
