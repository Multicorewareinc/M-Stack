import { useQuery } from '@tanstack/react-query';
import { listPlans } from '../../../api/plans';
import { queryKeys } from '../../../api/queryKeys';

export function usePlans() {
  return useQuery({ queryKey: queryKeys.plans(), queryFn: () => listPlans() });
}
