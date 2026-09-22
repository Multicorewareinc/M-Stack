import { Badge, Button, DataTable, PageHeader, SearchInput, type Column, type RowAction } from '@multistack/ui';
import * as React from 'react';
import type { Permission } from '../../api/types';
import { DeactivatePermissionDialog } from './DeactivatePermissionDialog';
import { useActivatePermission } from './hooks/useActivatePermission';
import { usePermissions } from './hooks/usePermissions';
import { PermissionFormDrawer } from './PermissionFormDrawer';

export function PermissionsPage() {
  const [search, setSearch] = React.useState('');
  const [formOpen, setFormOpen] = React.useState(false);
  const [editTarget, setEditTarget] = React.useState<Permission | undefined>(undefined);
  const [deactivateTarget, setDeactivateTarget] = React.useState<Permission | null>(null);

  const { data: permissions, isLoading, error, refetch } = usePermissions();
  const activatePermission = useActivatePermission();

  const filtered = React.useMemo(() => {
    if (!permissions) return [];
    if (!search) return permissions;
    const q = search.toLowerCase();
    return permissions.filter(
      (p) => p.resource.toLowerCase().includes(q) || p.action.toLowerCase().includes(q) || p.slug.toLowerCase().includes(q),
    );
  }, [permissions, search]);

  const columns: Column<Permission>[] = [
    { key: 'resource', header: 'Resource', accessor: (p) => p.resource },
    { key: 'action', header: 'Action', accessor: (p) => p.action },
    { key: 'slug', header: 'Permission', render: (p) => <span className="font-mono">{p.slug}</span> },
    { key: 'description', header: 'Description', accessor: (p) => p.description ?? '—' },
    {
      key: 'status',
      header: 'Status',
      render: (p) => <Badge variant={p.is_active ? 'success' : 'neutral'}>{p.is_active ? 'Active' : 'Inactive'}</Badge>,
    },
  ];

  function openCreate() {
    setEditTarget(undefined);
    setFormOpen(true);
  }
  function openEdit(permission: Permission) {
    setEditTarget(permission);
    setFormOpen(true);
  }

  return (
    <div>
      <PageHeader
        eyebrow="Platform Control Plane / RBAC"
        title="Master Permission Catalog"
        description="These permissions are defined at the platform level. Organization roles can compose roles from this catalog. Only Super Admins can modify the master permission list."
        actions={<Button onClick={openCreate}>+ Create Permission</Button>}
      />
      <SearchInput
        aria-label="Search permissions"
        placeholder="Search permissions..."
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
          const actions: RowAction<Permission>[] = [{ label: 'Edit', onClick: () => openEdit(p) }];
          if (p.is_active) {
            actions.push({ label: 'Deactivate', destructive: true, onClick: () => setDeactivateTarget(p) });
          } else {
            actions.push({ label: 'Reactivate', onClick: () => activatePermission.mutate(p.id) });
          }
          return actions;
        }}
        empty={{ title: 'No permissions yet', description: 'Create a permission to add it to the master catalog.' }}
      />
      <PermissionFormDrawer open={formOpen} onOpenChange={setFormOpen} permission={editTarget} />
      {deactivateTarget && (
        <DeactivatePermissionDialog
          open={Boolean(deactivateTarget)}
          onOpenChange={(open) => !open && setDeactivateTarget(null)}
          permission={deactivateTarget}
        />
      )}
    </div>
  );
}
