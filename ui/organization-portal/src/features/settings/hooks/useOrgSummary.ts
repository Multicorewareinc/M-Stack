import { useQuery } from '@tanstack/react-query';
import { getOrgSummary } from '../../../api/organization';
import { queryKeys } from '../../../api/queryKeys';
import { useOrgId } from '../../users/hooks/useOrgId';

export function useOrgSummary() {
  const orgId = useOrgId();
  return useQuery({ queryKey: queryKeys.organization(orgId), queryFn: () => getOrgSummary() });
}
