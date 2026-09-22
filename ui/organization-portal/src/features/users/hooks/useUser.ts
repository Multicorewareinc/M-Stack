import { useQuery } from '@tanstack/react-query';
import { getUser } from '../../../api/users';
import { queryKeys } from '../../../api/queryKeys';
import { useOrgId } from './useOrgId';

export function useUser(userId: string) {
  const orgId = useOrgId();
  return useQuery({
    queryKey: queryKeys.user(orgId, userId),
    queryFn: () => getUser(userId),
    enabled: Boolean(userId),
    retry: false,
  });
}
