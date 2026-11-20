import { Badge, DataTable, PageHeader, SearchInput, Select, StatCard, type Column } from '@multistack/ui';
import * as React from 'react';
import type { Deployment } from '../../api/client';
import { deploymentMeta, formatDateTime, healthMeta, timeAgo } from '../../lib/format';
import { useNow } from '../../lib/useNow';
import { StateGate } from '../state/StateGate';
import { useDeployments, useSummary } from '../state/hooks';
import { useSelectedDeployment } from './DeploymentDetail';

const ALL = 'all';
const SEARCH_DEBOUNCE_MS = 300;
const IN_PROGRESS = ['pending', 'validating', 'provisioning', 'deleting'];

function DeploymentsView() {
  const [searchInput, setSearchInput] = React.useState('');
  const [search, setSearch] = React.useState('');
  const [status, setStatus] = React.useState(ALL);
  const [componentType, setComponentType] = React.useState(ALL);
  const { select } = useSelectedDeployment();
  const now = useNow();

  React.useEffect(() => {
    const t = setTimeout(() => setSearch(searchInput.trim()), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [searchInput]);

  const { data: summary } = useSummary();
  const { data: rows, isLoading, error, refetch } = useDeployments({
    search: search || undefined,
    status: status === ALL ? undefined : status,
    componentType: componentType === ALL ? undefined : componentType,
  });

  const count = (statuses: string[]) =>
    (summary?.by_status ?? []).filter((s) => statuses.includes(s.status)).reduce((n, s) => n + s.count, 0);

  const columns: Column<Deployment>[] = [
    { key: 'name', header: 'Name', render: (d) => <span className="font-medium">{d.name}</span> },
    { key: 'layer', header: 'Layer', accessor: (d) => d.component_type },
    {
      key: 'status',
      header: 'Status',
      render: (d) => <Badge variant={deploymentMeta(d.status).variant}>{deploymentMeta(d.status).label}</Badge>,
    },
    {
      key: 'health',
      header: 'Latest result',
      render: (d) => {
        const result = d.latest_health_check;
        if (!result) return '—';
        // A new attempt (re-deploy after a failure, a delete) is under way, so the stored result belongs to
        // the PREVIOUS attempt. Showing it as if current ("Provisioning" next to "Unhealthy") misleads.
        if (IN_PROGRESS.includes(d.status)) {
          return (
            <span className="text-neutral-500" title="Result of the previous attempt; the current one has not finished">
              was {healthMeta(result.status).label}
            </span>
          );
        }
        return <Badge variant={healthMeta(result.status).variant}>{healthMeta(result.status).label}</Badge>;
      },
    },
    {
      key: 'updated',
      header: 'Last updated',
      render: (d) => (
        <span title={formatDateTime(d.updated_at)}>
          {timeAgo(d.updated_at, now)}
        </span>
      ),
    },
    {
      key: 'error',
      header: 'Error',
      render: (d) =>
        d.error ? <span className="line-clamp-1 max-w-sm text-danger-700" title={d.error}>{d.error}</span> : '—',
    },
  ];

  const toggle = (value: string) => setStatus((current) => (current === value ? ALL : value));

  return (
    <div>
      <PageHeader
        title="Deployments"
        description="Everything the SDK has deployed on this machine and the state it is in."
      />

      <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-5">
        <StatCard label="Total" value={summary?.total ?? '—'} onClick={() => setStatus(ALL)} selected={status === ALL} />
        <StatCard
          label="Provisioned"
          value={count(['provisioned'])}
          onClick={() => toggle('provisioned')}
          selected={status === 'provisioned'}
        />
        <StatCard
          label="In progress"
          value={count(IN_PROGRESS)}
          hint="pending, validating, provisioning, deleting"
        />
        <StatCard label="Degraded" value={count(['degraded'])} onClick={() => toggle('degraded')} selected={status === 'degraded'} />
        <StatCard label="Failed" value={count(['failed'])} onClick={() => toggle('failed')} selected={status === 'failed'} />
      </div>

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <SearchInput
          aria-label="Search deployments"
          placeholder="Search name or error..."
          onValueChange={setSearchInput}
          className="max-w-sm"
        />
        <Select
          aria-label="Filter by layer"
          value={componentType}
          onValueChange={setComponentType}
          options={[
            { value: ALL, label: 'All layers' },
            ...(summary?.by_component_type ?? []).map((c) => ({ value: c.component_type, label: `${c.component_type} (${c.count})` })),
          ]}
          className="w-52"
        />
        <Select
          aria-label="Filter by status"
          value={status}
          onValueChange={setStatus}
          options={[
            { value: ALL, label: 'All statuses' },
            ...(summary?.by_status ?? []).map((s) => ({ value: s.status, label: `${deploymentMeta(s.status).label} (${s.count})` })),
          ]}
          className="w-52"
        />
      </div>

      <DataTable
        columns={columns}
        rows={rows ?? []}
        getRowId={(d) => d.name}
        loading={isLoading}
        error={error ? error.message : null}
        onRetry={() => refetch()}
        onRowClick={(d) => select(d.name)}
        empty={{ title: 'No deployments match', description: 'Clear the filters to see everything.' }}
      />
    </div>
  );
}

export function DeploymentsPage() {
  return (
    <StateGate>
      <DeploymentsView />
    </StateGate>
  );
}
