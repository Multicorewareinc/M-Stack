import { useQuery } from '@tanstack/react-query';
import { getUsageTimeseries } from '../../../api/usage';
import { queryKeys } from '../../../api/queryKeys';
import { useOrgId } from '../../users/hooks/useOrgId';

export function useUsageTimeseries() {
  const orgId = useOrgId();
  return useQuery({
    queryKey: queryKeys.usageTimeseries(orgId),
    queryFn: () => getUsageTimeseries(),
  });
}
