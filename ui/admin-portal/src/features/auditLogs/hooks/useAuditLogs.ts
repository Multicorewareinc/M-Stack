import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { listAuditLogs, type AuditLogFilters } from '../../../api/auditLogs';
import { queryKeys } from '../../../api/queryKeys';

export const AUDIT_PAGE_SIZE = 25;

export function useAuditLogs(filters: AuditLogFilters, offset: number) {
  return useQuery({
    queryKey: queryKeys.auditLogs(
      filters.organizationId,
      filters.action,
      filters.since,
      filters.until,
      filters.search,
      offset,
    ),
    queryFn: () => listAuditLogs(filters, AUDIT_PAGE_SIZE, offset),
    // Keep the current rows on screen while the next page / filter loads.
    placeholderData: keepPreviousData,
  });
}
