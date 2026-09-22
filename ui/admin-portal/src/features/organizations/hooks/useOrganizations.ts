import { useQuery } from '@tanstack/react-query';
import { listOrganizations } from '../../../api/organizations';
import { queryKeys } from '../../../api/queryKeys';

export function useOrganizations(search = '') {
  return useQuery({
    queryKey: queryKeys.organizations(search),
    queryFn: () => listOrganizations(search),
  });
}
