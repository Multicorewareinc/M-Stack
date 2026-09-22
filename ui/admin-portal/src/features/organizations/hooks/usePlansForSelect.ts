import { useQuery } from '@tanstack/react-query';
import { listPlans } from '../../../api/plans';
import { queryKeys } from '../../../api/queryKeys';

/** Read-only plans lookup for the plan picker/column here; full Plans CRUD is a later spec. */
export function usePlansForSelect() {
  return useQuery({ queryKey: queryKeys.plans(), queryFn: () => listPlans() });
}
