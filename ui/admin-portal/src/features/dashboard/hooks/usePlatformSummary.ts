import { useQuery } from '@tanstack/react-query';
import { getPlatformSummary } from '../../../api/dashboard';
import { queryKeys } from '../../../api/queryKeys';

export function usePlatformSummary() {
  return useQuery({ queryKey: queryKeys.platformSummary(), queryFn: () => getPlatformSummary() });
}
