import { useQuery } from '@tanstack/react-query';
import { Badge, Breadcrumbs, Button, EmptyState, FormField, PageHeader, Select, StatusBadge, Tabs, TabsContent, TabsList, TabsTrigger } from '@multistack/ui';
import * as React from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ApiError } from '../../api/client';
import { getUserEffectivePermissions } from '../../api/users';
import { listPermissions } from '../../api/permissions';
import { Can } from '../../components/Can';
import { useToast } from '../../app/ToastProvider';
import { formatDate } from '../../lib/formatters';
import { EditUserDrawer } from './EditUserDrawer';
import { ResetUserPasswordDrawer } from './ResetUserPasswordDrawer';
import { useUser } from './hooks/useUser';
import { useSetUserRoles, useUserRoles } from './hooks/useUserRoles';
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
  const navigate = useNavigate();
  const { data: user, isLoading, isError } = useUser(userId);
  const { data: userRoles } = useUserRoles(userId);
  const { data: roles } = useRolesForSelect();
  const { data: effective } = useQuery({
    queryKey: ['effective-permissions', userId],
    queryFn: () => getUserEffectivePermissions(userId),
    enabled: Boolean(user),
  });
  const { data: catalog } = useQuery({ queryKey: ['permissions-catalog'], queryFn: () => listPermissions() });
  const setUserRoles = useSetUserRoles(userId);
  const toast = useToast();

  const [editOpen, setEditOpen] = React.useState(false);
  const [resetPasswordOpen, setResetPasswordOpen] = React.useState(false);

  // Single-select, mirroring CreateUserDrawer's "one role per user" rule (see its own comment) —
  // a user's role set here is always 0 or 1 ids, never several.
  const [roleId, setRoleId] = React.useState('');
  // The dropdown starts read-only (just the current role shown) — "Edit Role" makes it
  // accessible, "Save Roles"/"Cancel" leave edit mode again.
  const [editingRole, setEditingRole] = React.useState(false);
  // Tracks which role-id set roleId was last synced from, so a subsequent refetch with the same
  // data doesn't clobber an in-progress, not-yet-saved selection.
  const syncedForRef = React.useRef<string | null>(null);
  React.useLayoutEffect(() => {
    if (!userRoles) return;
    const key = userRoles.role_ids.join(',');
    if (syncedForRef.current !== key) {
      setRoleId(userRoles.role_ids[0] ?? '');
      syncedForRef.current = key;
    }
  }, [userRoles]);

  async function handleSaveRole() {
    try {
      await setUserRoles.mutateAsync(roleId ? [roleId] : []);
      setEditingRole(false);
      toast.success('Role updated');
    } catch (err) {
      toast.error('Unable to update role', err instanceof ApiError ? err.message : undefined);
    }
  }

  function handleCancelEditRole() {
    setRoleId(userRoles?.role_ids[0] ?? '');
    setEditingRole(false);
  }

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
      <Breadcrumbs
        className="mb-2"
        items={[{ label: 'Users', href: '/users' }, { label: user.display_name ?? user.username }]}
        onNavigate={navigate}
      />
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
          <Can
            permission="users.update"
            fallback={
              assignedRoles.length === 0 ? (
                <EmptyState title="No roles assigned" description="This user has no role assigned." />
              ) : (
                <Badge variant="primary">{assignedRoles[0].name}</Badge>
              )
            }
          >
            {editingRole ? (
              <div className="flex max-w-sm flex-col gap-3">
                <FormField label="Role" htmlFor="user-detail-role" hint="This user gets exactly one role.">
                  <Select
                    id="user-detail-role"
                    value={roleId}
                    onValueChange={setRoleId}
                    options={(roles ?? []).map((r) => ({ value: r.id, label: r.name }))}
                    placeholder="No role"
                  />
                </FormField>
                <div className="flex gap-2">
                  <Button
                    className="w-fit"
                    disabled={roleId === (userRoles?.role_ids[0] ?? '')}
                    onClick={handleSaveRole}
                    loading={setUserRoles.isPending}
                  >
                    Save Roles
                  </Button>
                  <Button variant="secondary" className="w-fit" onClick={handleCancelEditRole}>
                    Cancel
                  </Button>
                </div>
              </div>
            ) : (
              <div className="flex flex-col items-start gap-3">
                {assignedRoles.length === 0 ? (
                  <p className="text-body text-neutral-500">No role assigned.</p>
                ) : (
                  <Badge variant="primary">{assignedRoles[0].name}</Badge>
                )}
                <Button variant="secondary" className="w-fit" onClick={() => setEditingRole(true)}>
                  Edit Role
                </Button>
              </div>
            )}
          </Can>
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
      <ResetUserPasswordDrawer open={resetPasswordOpen} onOpenChange={setResetPasswordOpen} user={user} />
    </div>
  );
}
