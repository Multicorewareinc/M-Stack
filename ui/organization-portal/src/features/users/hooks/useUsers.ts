import { useQuery } from '@tanstack/react-query';
import { listUsers } from '../../../api/users';
import { queryKeys } from '../../../api/queryKeys';
import { useOrgId } from './useOrgId';

export function useUsers() {
  const orgId = useOrgId();
  return useQuery({ queryKey: queryKeys.users(orgId), queryFn: () => listUsers() });
}
