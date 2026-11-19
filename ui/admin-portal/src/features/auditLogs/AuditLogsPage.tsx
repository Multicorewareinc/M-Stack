import { Badge, Button, DataTable, Input, PageHeader, SearchInput, Select, type Column } from '@multistack/ui';
import * as React from 'react';
import type { AuditLog } from '../../api/types';
import { formatDate } from '../../lib/formatters';
import { useOrganizations } from '../organizations/hooks/useOrganizations';
import { AuditLogDetailDialog } from './AuditLogDetailDialog';
import { actionLabel, actionVariant, actorLabel, AUDIT_ACTIONS } from './auditActions';
import { AUDIT_PAGE_SIZE, useAuditLogs } from './hooks/useAuditLogs';
import {
  isCustomRangeInvalid,
  resolveTimeBounds,
  TIME_RANGES,
  type TimeRangePreset,
} from './timeRange';

const ALL = 'all';
const SEARCH_DEBOUNCE_MS = 300;

/**
 * Platform-wide audit trail (Super Admin / Admin): every organization's user, role and API-key
 * activity plus the platform's own org / plan / permission / admin-auth events, newest first. The
 * org-scoped view lives in the Organization Portal.
 */
export function AuditLogsPage() {
  const [searchInput, setSearchInput] = React.useState('');
  const [search, setSearch] = React.useState('');
  const [action, setAction] = React.useState(ALL);
  const [range, setRange] = React.useState<TimeRangePreset>('all');
  const [fromDate, setFromDate] = React.useState('');
  const [toDate, setToDate] = React.useState('');
  const [selected, setSelected] = React.useState<AuditLog | null>(null);
  const [organizationId, setOrganizationId] = React.useState(ALL);
  const [offset, setOffset] = React.useState(0);

  // Debounce typing so each keystroke doesn't fire a request. A changed search returns to page 1;
  // an unchanged one (e.g. the timer armed at mount) must not undo the user's paging.
  React.useEffect(() => {
    const t = setTimeout(() => {
      const next = searchInput.trim();
      if (next === search) return;
      setSearch(next);
      setOffset(0);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [searchInput, search]);

  // Rolling presets are measured from the moment they are picked (not on every render), so the
  // query key stays stable until the range, or a custom date, changes.
  const bounds = React.useMemo(
    () => resolveTimeBounds(range, fromDate, toDate, new Date()),
    [range, fromDate, toDate],
  );
  const customRangeInvalid = range === 'custom' && isCustomRangeInvalid(fromDate, toDate);

  const { data, isLoading, error, refetch, isFetching } = useAuditLogs(
    {
      search: search || undefined,
      action: action === ALL ? undefined : action,
      since: bounds.since,
      until: bounds.until,
      organizationId: organizationId === ALL ? undefined : organizationId,
    },
    offset,
  );
  const { data: organizations } = useOrganizations();

  const orgNameById = React.useMemo(
    () => new Map((organizations ?? []).map((o) => [o.id, o.name] as const)),
    [organizations],
  );

  const columns: Column<AuditLog>[] = [
    { key: 'time', header: 'Time', accessor: (l) => formatDate(l.created_at) },
    { key: 'actor', header: 'Actor', accessor: (l) => actorLabel(l) },
    {
      key: 'event',
      header: 'Event',
      render: (l) => <Badge variant={actionVariant(l.action)}>{actionLabel(l.action)}</Badge>,
    },
    { key: 'target', header: 'Target', accessor: (l) => l.resource_name ?? l.resource_id ?? '—' },
    {
      key: 'organization',
      header: 'Organization',
      accessor: (l) => (l.organization_id ? (orgNameById.get(l.organization_id) ?? l.organization_id) : 'Platform'),
    },
    {
      key: 'details',
      header: 'Details',
      render: (l) => (
        <Button
          variant="ghost"
          size="sm"
          aria-label={`View details for ${actionLabel(l.action)} at ${formatDate(l.created_at)}`}
          onClick={() => setSelected(l)}
        >
          View details
        </Button>
      ),
    },
  ];

  const total = data?.total ?? 0;
  const first = total === 0 ? 0 : offset + 1;
  const last = Math.min(offset + AUDIT_PAGE_SIZE, total);

  function changeFilter(set: (value: string) => void) {
    return (value: string) => {
      set(value);
      setOffset(0);
    };
  }

  return (
    <div>
      <PageHeader
        eyebrow="Platform Control Plane / Audit"
        title="Audit Logs"
        description="Who did what, across every organization and the platform itself."
      />
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <SearchInput
          aria-label="Search audit logs"
          placeholder="Search actor, target or event..."
          onValueChange={setSearchInput}
          className="max-w-sm"
        />
        <Select
          aria-label="Filter by organization"
          value={organizationId}
          onValueChange={changeFilter(setOrganizationId)}
          options={[
            { value: ALL, label: 'All organizations' },
            ...(organizations ?? []).map((o) => ({ value: o.id, label: o.name })),
          ]}
          className="w-52"
        />
        <Select
          aria-label="Filter by time range"
          value={range}
          onValueChange={(value) => {
            setRange(value as TimeRangePreset);
            setOffset(0);
          }}
          options={TIME_RANGES.map((r) => ({ value: r.value, label: r.label }))}
          className="w-48"
        />
        {range === 'custom' && (
          <>
            <Input
              type="date"
              aria-label="From date"
              value={fromDate}
              max={toDate || undefined}
              error={customRangeInvalid}
              onChange={(e) => {
                setFromDate(e.target.value);
                setOffset(0);
              }}
              className="w-40"
            />
            <Input
              type="date"
              aria-label="To date"
              value={toDate}
              min={fromDate || undefined}
              error={customRangeInvalid}
              onChange={(e) => {
                setToDate(e.target.value);
                setOffset(0);
              }}
              className="w-40"
            />
            {customRangeInvalid && (
              <span role="alert" className="text-secondary text-danger-700">
                "From" must be on or before "To".
              </span>
            )}
          </>
        )}
        <Select
          aria-label="Filter by event"
          value={action}
          onValueChange={changeFilter(setAction)}
          options={[
            { value: ALL, label: 'All events' },
            ...AUDIT_ACTIONS.map((a) => ({ value: a, label: actionLabel(a) })),
          ]}
          className="w-56"
        />
      </div>
      <DataTable
        columns={columns}
        rows={data?.items ?? []}
        getRowId={(l) => l.id}
        loading={isLoading}
        error={error ? error.message : null}
        onRetry={() => refetch()}
        empty={{ title: 'No audit events', description: 'Nothing matches these filters yet.' }}
      />
      <AuditLogDetailDialog
        log={selected}
        organizationName={selected?.organization_id ? orgNameById.get(selected.organization_id) : undefined}
        onClose={() => setSelected(null)}
      />
      <div className="mt-3 flex items-center justify-between text-body text-neutral-600">
        <span aria-live="polite">{total === 0 ? 'No events' : `Showing ${first}–${last} of ${total}`}</span>
        <div className="flex gap-2">
          <Button
            variant="secondary"
            disabled={offset === 0 || isFetching}
            onClick={() => setOffset(Math.max(0, offset - AUDIT_PAGE_SIZE))}
          >
            Previous
          </Button>
          <Button
            variant="secondary"
            disabled={offset + AUDIT_PAGE_SIZE >= total || isFetching}
            onClick={() => setOffset(offset + AUDIT_PAGE_SIZE)}
          >
            Next
          </Button>
        </div>
      </div>
    </div>
  );
}
