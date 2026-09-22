import { Button, DataTable, PageHeader, SearchInput, StatusBadge, type Column } from '@multistack/ui';
import * as React from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import type { Organization } from '../../api/types';
import { formatDate } from '../../lib/formatters';
import { getAvailableActions, type OrgActionKey } from './actions';
import { ChangePlanDrawer } from './ChangePlanDrawer';
import { CreateOrganizationDrawer } from './CreateOrganizationDrawer';
import { DeleteOrganizationDialog } from './DeleteOrganizationDialog';
import { useOrganizations } from './hooks/useOrganizations';
import { usePlansForSelect } from './hooks/usePlansForSelect';
import { SuspendOrganizationDialog } from './SuspendOrganizationDialog';
import { useRetryProvisioning } from './hooks/useRetryProvisioning';

export function OrganizationsPage() {
  const [search, setSearch] = React.useState('');
  const [createOpen, setCreateOpen] = React.useState(false);
  const [changePlanTarget, setChangePlanTarget] = React.useState<Organization | null>(null);
  const [suspendTarget, setSuspendTarget] = React.useState<Organization | null>(null);
  const [deleteTarget, setDeleteTarget] = React.useState<Organization | null>(null);
  const navigate = useNavigate();
  const location = useLocation();

  const { data: organizations, isLoading, error, refetch } = useOrganizations(search);
  const { data: plans } = usePlansForSelect();
  const retryProvisioning = useRetryProvisioning();

  const planName = (planId: string) => plans?.find((p) => p.id === planId)?.name ?? planId;

  // Opens as a popup over this list (see app/routes.tsx) rather than a full
  // navigation — the list stays visible/interactive underneath.
  function openDetail(id: string) {
    navigate(`/organizations/${id}`, { state: { backgroundLocation: location } });
  }

  const columns: Column<Organization>[] = [
    { key: 'name', header: 'Organization', accessor: (o) => o.name },
    { key: 'plan', header: 'Plan', accessor: (o) => planName(o.plan_id) },
    { key: 'users', header: 'Users', accessor: (o) => o.user_count },
    { key: 'status', header: 'Status', render: (o) => <StatusBadge status={o.status} /> },
    { key: 'created', header: 'Created', accessor: (o) => formatDate(o.created_at) },
  ];

  function handleAction(key: OrgActionKey, org: Organization) {
    if (key === 'view') openDetail(org.id);
    else if (key === 'change-plan') setChangePlanTarget(org);
    else if (key === 'suspend') setSuspendTarget(org);
    else if (key === 'retry') retryProvisioning.mutate(org.id);
    else if (key === 'delete') setDeleteTarget(org);
  }

  return (
    <div>
      <PageHeader
        eyebrow="Platform Control Plane / Organizations"
        title="Organizations"
        description="Manage organizations and their platform entitlements."
        actions={<Button onClick={() => setCreateOpen(true)}>+ Create Organization</Button>}
      />
      <SearchInput
        aria-label="Search organizations"
        placeholder="Search organizations..."
        onValueChange={setSearch}
        className="mb-4 max-w-sm"
      />
      <DataTable
        columns={columns}
        rows={organizations ?? []}
        getRowId={(o) => o.id}
        loading={isLoading}
        error={error ? error.message : null}
        onRetry={() => refetch()}
        onRowClick={(o) => openDetail(o.id)}
        rowActions={(o) =>
          getAvailableActions(o)
            .filter((a) => a.key !== 'view')
            .map((a) => ({
              label: a.label,
              destructive: a.destructive,
              onClick: () => handleAction(a.key, o),
            }))
        }
        empty={
          search
            ? { title: 'No organizations found', description: 'Try changing your search or filters.' }
            : {
                title: 'No organizations yet',
                description: 'Create your first organization to get started.',
                action: { label: 'Create Organization', onClick: () => setCreateOpen(true) },
              }
        }
      />
      <CreateOrganizationDrawer open={createOpen} onOpenChange={setCreateOpen} plans={plans ?? []} />
      {changePlanTarget && (
        <ChangePlanDrawer
          open={Boolean(changePlanTarget)}
          onOpenChange={(open) => !open && setChangePlanTarget(null)}
          organization={changePlanTarget}
          plans={plans ?? []}
        />
      )}
      {suspendTarget && (
        <SuspendOrganizationDialog
          open={Boolean(suspendTarget)}
          onOpenChange={(open) => !open && setSuspendTarget(null)}
          organization={suspendTarget}
        />
      )}
      {deleteTarget && (
        <DeleteOrganizationDialog
          open={Boolean(deleteTarget)}
          onOpenChange={(open) => !open && setDeleteTarget(null)}
          organization={deleteTarget}
        />
      )}
    </div>
  );
}
