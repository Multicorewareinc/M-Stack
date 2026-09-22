import { useQuery } from '@tanstack/react-query';
import { getUserDetail } from '../../../api/users';
import { queryKeys } from '../../../api/queryKeys';

/** Composed detail — Admin CP orchestrates a call to Org CP (§21, §55). */
export function useUserDetail(userId: string) {
  return useQuery({
    queryKey: queryKeys.user(userId),
    queryFn: () => getUserDetail(userId),
    enabled: Boolean(userId),
    retry: false,
  });
}
