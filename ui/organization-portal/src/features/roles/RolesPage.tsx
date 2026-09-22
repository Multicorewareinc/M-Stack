import { Badge, Button, DataTable, PageHeader, type Column, type RowAction } from '@multistack/ui';
import * as React from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import type { Role } from '../../api/types';
import { Can } from '../../components/Can';
import { DeleteRoleDialog } from './DeleteRoleDialog';
import { useRoles } from './hooks/useRoles';
import { RoleFormDrawer } from './RoleFormDrawer';

export function RolesPage() {
  const [createOpen, setCreateOpen] = React.useState(false);
  const [deleteTarget, setDeleteTarget] = React.useState<Role | null>(null);
  const navigate = useNavigate();
  const location = useLocation();
  const { data: roles, isLoading, error, refetch } = useRoles();

  const columns: Column<Role>[] = [
    {
      key: 'name',
      header: 'Role',
      render: (r) => (
        <span className="flex items-center gap-2">
          {r.name}
          {r.is_system_role && <Badge variant="neutral">System role</Badge>}
        </span>
      ),
    },
    { key: 'description', header: 'Description', accessor: (r) => r.description ?? '—' },
    { key: 'users', header: 'Users', accessor: (r) => r.user_count ?? '—' },
    { key: 'permissions', header: 'Permissions', accessor: (r) => r.permission_count ?? '—' },
  ];

  return (
    <div>
      <PageHeader
        title="Roles"
        description="Manage organization roles and their permissions."
        actions={
          <Can permission="roles.create">
            <Button onClick={() => setCreateOpen(true)}>+ Create Role</Button>
          </Can>
        }
      />
      <DataTable
        columns={columns}
        rows={roles ?? []}
        getRowId={(r) => r.id}
        loading={isLoading}
        error={error ? error.message : null}
        onRetry={() => refetch()}
        onRowClick={(r) => navigate(`/roles/${r.id}`, { state: { backgroundLocation: location } })}
        rowActions={(r) => {
          const actions: RowAction<Role>[] = [
            { label: 'View', onClick: () => navigate(`/roles/${r.id}`, { state: { backgroundLocation: location } }) },
          ];
          // System roles are never deletable from here either — matches the Role Detail page's
          // own Delete button.
          if (!r.is_system_role) {
            actions.push({ label: 'Delete', destructive: true, onClick: () => setDeleteTarget(r) });
          }
          return actions;
        }}
        empty={{ title: 'No roles yet', description: 'Create a role to define organization access.' }}
      />
      <RoleFormDrawer open={createOpen} onOpenChange={setCreateOpen} />
      {deleteTarget && (
        <DeleteRoleDialog
          open={Boolean(deleteTarget)}
          onOpenChange={(open) => !open && setDeleteTarget(null)}
          role={deleteTarget}
        />
      )}
    </div>
  );
}
