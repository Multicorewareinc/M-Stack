import { useQuery } from '@tanstack/react-query';
import { listPermissions } from '../../../api/permissions';
import { queryKeys } from '../../../api/queryKeys';

export function usePermissions() {
  return useQuery({ queryKey: queryKeys.permissions(), queryFn: () => listPermissions() });
}
