import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import { useRefresh } from '../../app/RefreshContext';

// Every query polls at the cadence chosen in the header, and keeps the previous rows on screen while
// a filter change loads so the table does not flash empty.

export function useInfo() {
  const { interval } = useRefresh();
  return useQuery({ queryKey: ['info'], queryFn: api.info, refetchInterval: interval });
}

export function useSummary() {
  const { interval } = useRefresh();
  return useQuery({ queryKey: ['summary'], queryFn: api.summary, refetchInterval: interval });
}

export function useDeployments(filters: { status?: string; componentType?: string; search?: string }) {
  const { interval } = useRefresh();
  return useQuery({
    queryKey: ['deployments', filters.status ?? '', filters.componentType ?? '', filters.search ?? ''],
    queryFn: () => api.deployments(filters),
    refetchInterval: interval,
    placeholderData: keepPreviousData,
  });
}

export function useDeployment(name: string | null) {
  const { interval } = useRefresh();
  return useQuery({
    queryKey: ['deployment', name],
    queryFn: () => api.deployment(name as string),
    enabled: name !== null,
    refetchInterval: interval,
    retry: false, // a 404 (deleted since the list loaded) is an answer, not a blip
  });
}

export function useHealthChecks(filters: { status?: string; limit?: number }) {
  const { interval } = useRefresh();
  return useQuery({
    queryKey: ['health-checks', filters.status ?? '', filters.limit ?? 100],
    queryFn: () => api.healthChecks(filters),
    refetchInterval: interval,
    placeholderData: keepPreviousData,
  });
}
