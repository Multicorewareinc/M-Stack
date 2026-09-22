import { useQuery } from '@tanstack/react-query';
import { listOrganizations } from '../../../api/organizations';
import { queryKeys } from '../../../api/queryKeys';

const RECENT_COUNT = 5;

/**
 * Derives "recent" client-side from the existing list call (D1) — no backend
 * "recent" endpoint exists yet; sorting a small mocked/V1-scale list here is
 * correct and simple. Revisit with a server-sorted endpoint once real data
 * volume requires it.
 */
export function useRecentOrganizations() {
  const query = useQuery({ queryKey: queryKeys.organizations(), queryFn: () => listOrganizations() });
  const recent = query.data
    ? [...query.data].sort((a, b) => b.updated_at.localeCompare(a.updated_at)).slice(0, RECENT_COUNT)
    : undefined;
  return { ...query, data: recent };
}
