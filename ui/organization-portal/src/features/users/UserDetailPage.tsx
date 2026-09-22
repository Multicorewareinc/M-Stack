import { useQuery } from '@tanstack/react-query';
import { Badge, Button, EmptyState, PageHeader, StatusBadge, Tabs, TabsContent, TabsList, TabsTrigger } from '@multistack/ui';
import * as React from 'react';
import { useParams } from 'react-router-dom';
import { getUserEffectivePermissions } from '../../api/users';
import { listPermissions } from '../../api/permissions';
import { Can } from '../../components/Can';
import { formatDate } from '../../lib/formatters';
import { EditUserDrawer } from './EditUserDrawer';
import { ManageRolesDrawer } from './ManageRolesDrawer';
import { ResetUserPasswordDrawer } from './ResetUserPasswordDrawer';
import { useUser } from './hooks/useUser';
import { useUserRoles } from './hooks/useUserRoles';
import { useRolesForSelect } from './hooks/useRolesForSelect';

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-secondary text-neutral-500">{label}</dt>
      <dd className="text-body text-neutral-900">{children}</dd>
    </div>
  );
}

export function UserDetailPage() {
  const { userId = '' } = useParams();
  const { data: user, isLoading, isError } = useUser(userId);
  const { data: userRoles } = useUserRoles(userId);
  const { data: roles } = useRolesForSelect();
  const { data: effective } = useQuery({
    queryKey: ['effective-permissions', userId],
    queryFn: () => getUserEffectivePermissions(userId),
    enabled: Boolean(user),
  });
  const { data: catalog } = useQuery({ queryKey: ['permissions-catalog'], queryFn: () => listPermissions() });

  const [editOpen, setEditOpen] = React.useState(false);
  const [rolesOpen, setRolesOpen] = React.useState(false);
  const [resetPasswordOpen, setResetPasswordOpen] = React.useState(false);

  if (isLoading) return <div>Loading…</div>;

  if (isError || !user) {
    return (
      <EmptyState title="User not found" description="The user may have been deleted or you may no longer have access." />
    );
  }

  const assignedRoles = (roles ?? []).filter((r) => userRoles?.role_ids.includes(r.id));
  const effectiveSlugs = (effective?.permission_ids ?? [])
    .map((id) => catalog?.find((p) => p.id === id)?.slug)
    .filter((s): s is string => Boolean(s));

  return (
    <div>
      <PageHeader
        title={user.display_name ?? user.username}
        actions={
          <Can permission="users.update">
            <div className="flex gap-2">
              <Button variant="secondary" onClick={() => setResetPasswordOpen(true)}>
                Reset Password
              </Button>
              <Button variant="secondary" onClick={() => setEditOpen(true)}>
                Edit
              </Button>
            </div>
          </Can>
        }
      />
      <div className="mb-4 flex items-center gap-2">
        <StatusBadge status={user.status} />
        <span className="text-secondary text-neutral-500">{user.email}</span>
      </div>

      <Tabs defaultValue="profile">
        <TabsList>
          <TabsTrigger value="profile">Profile</TabsTrigger>
          <TabsTrigger value="roles">Roles</TabsTrigger>
          <TabsTrigger value="permissions">Effective Permissions</TabsTrigger>
          <TabsTrigger value="activity">Activity</TabsTrigger>
        </TabsList>

        <TabsContent value="profile">
          <dl className="grid grid-cols-2 gap-4">
            <Field label="Username">{user.username}</Field>
            <Field label="First Name">{user.first_name ?? '—'}</Field>
            <Field label="Last Name">{user.last_name ?? '—'}</Field>
            <Field label="Email">{user.email}</Field>
            <Field label="Status">
              <StatusBadge status={user.status} />
            </Field>
          </dl>
        </TabsContent>

        <TabsContent value="roles">
          <div className="flex flex-col gap-3">
            {assignedRoles.length === 0 ? (
              <EmptyState
                title="No roles assigned"
                description="Assign a role to give this user organization permissions."
                action={{ label: 'Manage Roles', onClick: () => setRolesOpen(true) }}
              />
            ) : (
              <>
                <div className="flex flex-col gap-2">
                  {assignedRoles.map((r) => (
                    <Badge key={r.id} variant="primary">
                      {r.name}
                    </Badge>
                  ))}
                </div>
                <Can permission="users.update">
                  <Button variant="secondary" className="w-fit" onClick={() => setRolesOpen(true)}>
                    Manage Roles
                  </Button>
                </Can>
              </>
            )}
          </div>
        </TabsContent>

        <TabsContent value="permissions">
          <div className="flex flex-col gap-2">
            {effectiveSlugs.length === 0 ? (
              <EmptyState
                title="No effective permissions"
                description="This user currently has no permissions through assigned roles."
              />
            ) : (
              effectiveSlugs.map((slug) => (
                <span key={slug} className="font-mono text-body text-neutral-800">
                  {slug}
                </span>
              ))
            )}
          </div>
        </TabsContent>

        <TabsContent value="activity">
          <dl className="grid grid-cols-2 gap-4">
            <Field label="Created">{formatDate(user.created_at)}</Field>
            {user.last_login_at && <Field label="Last Login">{formatDate(user.last_login_at)}</Field>}
          </dl>
        </TabsContent>
      </Tabs>

      <EditUserDrawer open={editOpen} onOpenChange={setEditOpen} user={user} />
      <ManageRolesDrawer open={rolesOpen} onOpenChange={setRolesOpen} userId={user.id} />
      <ResetUserPasswordDrawer open={resetPasswordOpen} onOpenChange={setResetPasswordOpen} user={user} />
    </div>
  );
}
