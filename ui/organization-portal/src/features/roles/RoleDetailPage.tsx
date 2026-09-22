import { Badge, Button, EmptyState, PageHeader, StatCard, Tabs, TabsContent, TabsList, TabsTrigger } from '@multistack/ui';
import * as React from 'react';
import { useParams } from 'react-router-dom';
import { Can } from '../../components/Can';
import { DeleteRoleDialog } from './DeleteRoleDialog';
import { RoleFormDrawer } from './RoleFormDrawer';
import { RolePermissionsTab } from './RolePermissionsTab';
import { useRole } from './hooks/useRole';

export function RoleDetailPage() {
  const { roleId = '' } = useParams();
  const { data: role, isLoading, isError } = useRole(roleId);
  const [editOpen, setEditOpen] = React.useState(false);
  const [deleteOpen, setDeleteOpen] = React.useState(false);

  if (isLoading) return <div>Loading…</div>;

  if (isError || !role) {
    return <EmptyState title="Role not found" description="The role may have been deleted or you may no longer have access." />;
  }

  return (
    <div>
      <PageHeader
        title={role.name}
        description={role.description ?? undefined}
        actions={
          <div className="flex gap-2">
            <Can permission="roles.update">
              <Button variant="secondary" onClick={() => setEditOpen(true)}>
                Edit
              </Button>
            </Can>
            {!role.is_system_role && (
              <Can permission="roles.delete">
                <Button variant="destructive" onClick={() => setDeleteOpen(true)}>
                  Delete
                </Button>
              </Can>
            )}
          </div>
        }
      />
      {role.is_system_role && (
        <div className="mb-4">
          <Badge variant="neutral">System role</Badge>
        </div>
      )}

      <Tabs defaultValue="overview">
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="permissions">Permissions</TabsTrigger>
          <TabsTrigger value="users">Users</TabsTrigger>
        </TabsList>

        <TabsContent value="overview">
          <div className="grid grid-cols-2 gap-4">
            <StatCard label="Users" value={role.user_count ?? '—'} />
            <StatCard label="Permissions" value={role.permission_count ?? '—'} />
          </div>
        </TabsContent>

        <TabsContent value="permissions">
          <RolePermissionsTab roleId={role.id} />
        </TabsContent>

        <TabsContent value="users">
          <p className="text-secondary text-neutral-500">{role.user_count ?? 0} users assigned to this role.</p>
        </TabsContent>
      </Tabs>

      <RoleFormDrawer open={editOpen} onOpenChange={setEditOpen} role={role} />
      <DeleteRoleDialog open={deleteOpen} onOpenChange={setDeleteOpen} role={role} />
    </div>
  );
}
