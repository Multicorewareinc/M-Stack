import { useQuery } from '@tanstack/react-query';
import { getOrganization } from '../../../api/organizations';
import { queryKeys } from '../../../api/queryKeys';

export function useOrganization(id: string) {
  return useQuery({
    queryKey: queryKeys.organization(id),
    queryFn: () => getOrganization(id),
    enabled: Boolean(id),
    retry: false,
  });
}
