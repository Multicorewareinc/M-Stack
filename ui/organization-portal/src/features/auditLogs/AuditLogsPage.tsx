import { Badge, Button, DataTable, EmptyState, Input, PageHeader, SearchInput, Select, type Column } from '@multistack/ui';
import * as React from 'react';
import type { AuditLog } from '../../api/auditLogs';
import { shouldUseRealAuth } from '../../auth/env';
import { formatDate } from '../../lib/formatters';
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

function AuditLogsTable() {
  const [searchInput, setSearchInput] = React.useState('');
  const [search, setSearch] = React.useState('');
  const [action, setAction] = React.useState(ALL);
  const [range, setRange] = React.useState<TimeRangePreset>('all');
  const [fromDate, setFromDate] = React.useState('');
  const [toDate, setToDate] = React.useState('');
  const [selected, setSelected] = React.useState<AuditLog | null>(null);
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
    },
    offset,
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

  return (
    <div>
      <PageHeader
        title="Audit Logs"
        description="A record of who did what in this organization — users, roles, API keys and sign-ins."
      />
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <SearchInput
          aria-label="Search audit logs"
          placeholder="Search actor, target or event..."
          onValueChange={setSearchInput}
          className="max-w-sm"
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
          onValueChange={(value) => {
            setAction(value);
            setOffset(0);
          }}
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
        empty={{ title: 'No audit events', description: 'Activity in this organization will appear here.' }}
      />
      <AuditLogDetailDialog log={selected} onClose={() => setSelected(null)} />
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

/**
 * Backed by modules/audit on organization-control-plane — an end-user JWT surface scoped to the
 * signed-in user's own organization server-side. Like API Keys it needs a real session (nothing for
 * this page to call under MockAuthProvider's shared development key), so it is gated on
 * shouldUseRealAuth() rather than silently 401ing.
 */
export function AuditLogsPage() {
  if (!shouldUseRealAuth()) {
    return (
      <div>
        <PageHeader title="Audit Logs" />
        <EmptyState
          title="Requires a real signed-in session"
          description="The audit trail is tied to your own identity and organization, not the shared development key this environment is currently using. Sign in with VITE_REAL_AUTH enabled to view it."
        />
      </div>
    );
  }
  return <AuditLogsTable />;
}
