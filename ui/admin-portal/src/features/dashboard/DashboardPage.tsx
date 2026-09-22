import { Button, DataTable, PageHeader, StatCard, StatusBadge, type Column } from '@multistack/ui';
import * as React from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import type { Organization } from '../../api/types';
import { usePlansForSelect } from '../organizations/hooks/usePlansForSelect';
import { usePlatformSummary } from './hooks/usePlatformSummary';
import { useRecentOrganizations } from './hooks/useRecentOrganizations';

type DashboardStatusFilter = 'provisioning' | 'active' | 'failed';

const STATUS_FILTER_LABEL: Record<DashboardStatusFilter, string> = {
  provisioning: 'Provisioning',
  active: 'Active',
  failed: 'Failed',
};

/**
 * Platform overview: counts + status breakdown + recent entities only — no
 * charts (§125) — and never a substitute for the full Organizations page
 * (§31), which it links to rather than replicates.
 */
export function DashboardPage() {
  const { data: summary, isLoading: summaryLoading } = usePlatformSummary();
  const { data: recentOrgs, isLoading: recentLoading, error: recentError } = useRecentOrganizations();
  const { data: plans } = usePlansForSelect();
  const navigate = useNavigate();
  const location = useLocation();
  const [statusFilter, setStatusFilter] = React.useState<DashboardStatusFilter | null>(null);

  const planName = (planId: string) => plans?.find((p) => p.id === planId)?.name ?? planId;

  function toggleStatusFilter(status: DashboardStatusFilter) {
    setStatusFilter((prev) => (prev === status ? null : status));
  }

  const visibleOrgs = statusFilter ? (recentOrgs ?? []).filter((o) => o.status === statusFilter) : recentOrgs;

  const columns: Column<Organization>[] = [
    { key: 'name', header: 'Organization', accessor: (o) => o.name },
    { key: 'plan', header: 'Plan', accessor: (o) => planName(o.plan_id) },
    { key: 'users', header: 'Users', accessor: (o) => o.user_count },
    { key: 'status', header: 'Status', render: (o) => <StatusBadge status={o.status} /> },
  ];

  return (
    <div>
      <PageHeader
        eyebrow="Platform Control Plane / Overview"
        title="Overview"
        actions={
          <Button variant="secondary" onClick={() => navigate('/organizations')}>
            View all organizations
          </Button>
        }
      />

      <div className="mb-8 grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
        <StatCard
          label="Organizations"
          value={summaryLoading ? '—' : summary?.organizations ?? 0}
          onClick={() => navigate('/organizations')}
        />
        <StatCard
          label="Active Users"
          value={summaryLoading ? '—' : summary?.active_users ?? 0}
          onClick={() => navigate('/users')}
        />
        <StatCard label="Plans" value={summaryLoading ? '—' : summary?.plans ?? 0} onClick={() => navigate('/plans')} />
        <StatCard
          label="Provisioning"
          value={summaryLoading ? '—' : summary?.provisioning ?? 0}
          onClick={() => toggleStatusFilter('provisioning')}
          selected={statusFilter === 'provisioning'}
        />
        <StatCard
          label="Active"
          value={summaryLoading ? '—' : summary?.active ?? 0}
          onClick={() => toggleStatusFilter('active')}
          selected={statusFilter === 'active'}
        />
        <StatCard
          label="Failed"
          value={summaryLoading ? '—' : summary?.failed ?? 0}
          onClick={() => toggleStatusFilter('failed')}
          selected={statusFilter === 'failed'}
        />
      </div>

      <div className="mb-3 flex items-center gap-2">
        <h2 className="text-cardTitle font-semibold text-neutral-900">
          {statusFilter ? `Recent Organizations — ${STATUS_FILTER_LABEL[statusFilter]}` : 'Recent Organizations'}
        </h2>
        {statusFilter && (
          <Button variant="ghost" size="sm" onClick={() => setStatusFilter(null)}>
            Clear filter
          </Button>
        )}
      </div>
      <DataTable
        columns={columns}
        rows={visibleOrgs ?? []}
        getRowId={(o) => o.id}
        loading={recentLoading}
        error={recentError ? recentError.message : null}
        onRowClick={(o) => navigate(`/organizations/${o.id}`, { state: { backgroundLocation: location } })}
        empty={{
          title: statusFilter ? `No ${statusFilter} organizations in the recent list` : 'No organizations yet',
        }}
      />
    </div>
  );
}
