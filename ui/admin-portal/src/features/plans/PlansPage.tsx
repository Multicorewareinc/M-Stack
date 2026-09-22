import { Badge, Button, DataTable, PageHeader, SearchInput, StatCard, type Column, type RowAction } from '@multistack/ui';
import * as React from 'react';
import type { Plan } from '../../api/types';
import { formatQuota } from '../../lib/formatters';
import { DeactivatePlanDialog } from './DeactivatePlanDialog';
import { useActivatePlan } from './hooks/useActivatePlan';
import { usePlans } from './hooks/usePlans';
import { PlanFormDrawer } from './PlanFormDrawer';

export function PlansPage() {
  const [search, setSearch] = React.useState('');
  const [formOpen, setFormOpen] = React.useState(false);
  const [editTarget, setEditTarget] = React.useState<Plan | undefined>(undefined);
  const [deactivateTarget, setDeactivateTarget] = React.useState<Plan | null>(null);

  const { data: plans, isLoading, error, refetch } = usePlans();
  const activatePlan = useActivatePlan();

  const filtered = React.useMemo(() => {
    if (!plans) return [];
    if (!search) return plans;
    const q = search.toLowerCase();
    return plans.filter((p) => p.name.toLowerCase().includes(q));
  }, [plans, search]);

  const columns: Column<Plan>[] = [
    { key: 'name', header: 'Plan', accessor: (p) => p.name },
    { key: 'tpm', header: 'TPM', accessor: (p) => formatQuota(p.tpm) },
    { key: 'rpm', header: 'RPM', accessor: (p) => formatQuota(p.rpm) },
    { key: 'quota', header: 'Quota', accessor: (p) => formatQuota(p.quota_monthly_tokens) },
    {
      key: 'status',
      header: 'Status',
      render: (p) => (
        <Badge variant={p.is_active ? 'success' : 'neutral'}>{p.is_active ? 'Active' : 'Inactive'}</Badge>
      ),
    },
  ];

  function openCreate() {
    setEditTarget(undefined);
    setFormOpen(true);
  }
  function openEdit(plan: Plan) {
    setEditTarget(plan);
    setFormOpen(true);
  }

  const activeCount = plans?.filter((p) => p.is_active).length ?? 0;
  const defaultCount = plans?.filter((p) => p.is_default).length ?? 0;

  return (
    <div>
      <PageHeader
        eyebrow="Platform Control Plane / Entitlements & Rate Limits"
        title="Plans"
        description="Define platform entitlements for organizations."
        actions={<Button onClick={openCreate}>+ Create Plan</Button>}
      />
      <div className="mb-6 grid grid-cols-2 gap-4 sm:grid-cols-3">
        <StatCard label="Total Plans" value={isLoading ? '—' : (plans?.length ?? 0)} />
        <StatCard label="Active" value={isLoading ? '—' : activeCount} />
        <StatCard label="Default Plans" value={isLoading ? '—' : defaultCount} />
      </div>
      <SearchInput
        aria-label="Search plans"
        placeholder="Search plans..."
        onValueChange={setSearch}
        className="mb-4 max-w-sm"
      />
      <DataTable
        columns={columns}
        rows={filtered}
        getRowId={(p) => p.id}
        loading={isLoading}
        error={error ? error.message : null}
        onRetry={() => refetch()}
        rowActions={(p) => {
          const actions: RowAction<Plan>[] = [{ label: 'Edit', onClick: () => openEdit(p) }];
          if (p.is_active) {
            actions.push({ label: 'Deactivate', destructive: true, onClick: () => setDeactivateTarget(p) });
          } else {
            actions.push({ label: 'Reactivate', onClick: () => activatePlan.mutate(p.id) });
          }
          return actions;
        }}
        empty={{ title: 'No plans yet', description: 'Create a plan to define platform entitlements.' }}
      />
      <PlanFormDrawer open={formOpen} onOpenChange={setFormOpen} plan={editTarget} />
      {deactivateTarget && (
        <DeactivatePlanDialog
          open={Boolean(deactivateTarget)}
          onOpenChange={(open) => !open && setDeactivateTarget(null)}
          plan={deactivateTarget}
        />
      )}
    </div>
  );
}
