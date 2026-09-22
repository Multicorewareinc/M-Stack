import { useQuery, type Query } from '@tanstack/react-query';
import { getOrganization } from '../../../api/organizations';
import { queryKeys } from '../../../api/queryKeys';
import type { Organization, OrganizationStatus } from '../../../api/types';

const TERMINAL_STATUSES: ReadonlySet<OrganizationStatus> = new Set(['active', 'failed']);
const POLL_INTERVAL_MS = 2000;

/**
 * Pure decision function: never poll once a terminal state is reached (§223).
 * Exported and unit-testable independent of React Query's timer plumbing.
 */
export function provisioningRefetchInterval(status: OrganizationStatus | undefined): number | false {
  if (!status || TERMINAL_STATUSES.has(status)) return false;
  return POLL_INTERVAL_MS;
}

export function useProvisioningStatus(organizationId: string) {
  return useQuery({
    queryKey: queryKeys.organization(organizationId),
    queryFn: () => getOrganization(organizationId),
    enabled: Boolean(organizationId),
    retry: false,
    refetchInterval: (query: Query<Organization>) => provisioningRefetchInterval(query.state.data?.status),
  });
}
