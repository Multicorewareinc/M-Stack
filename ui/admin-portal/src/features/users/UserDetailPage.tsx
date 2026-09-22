import { Badge, EmptyState, PageHeader, StatusBadge, Tabs, TabsContent, TabsList, TabsTrigger } from '@multistack/ui';
import * as React from 'react';
import { useParams } from 'react-router-dom';
import { formatDate } from '../../lib/formatters';
import { useUserDetail } from './hooks/useUserDetail';

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
  const { data: user, isLoading, isError } = useUserDetail(userId);

  if (isLoading) {
    // Distinct from the directory's own loading state — this is the
    // authoritative Admin CP -> Org CP fetch, not the fast projection (§43, §142-144).
    return <div data-testid="user-detail-loading">Loading latest user information…</div>;
  }

  if (isError || !user) {
    return (
      <EmptyState
        title="User not found"
        description="The user may have been deleted or you may no longer have access."
      />
    );
  }

  return (
    <div>
      <PageHeader eyebrow="Platform Control Plane / Users" title={user.display_name ?? user.username} />
      <div className="mb-4 flex items-center gap-2">
        <StatusBadge status={user.status} />
        <span className="text-secondary text-neutral-500">{user.email}</span>
      </div>

      <Tabs defaultValue="overview">
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="roles">Roles</TabsTrigger>
          <TabsTrigger value="permissions">Effective Permissions</TabsTrigger>
        </TabsList>

        <TabsContent value="overview">
          <dl className="grid grid-cols-2 gap-4">
            <Field label="Username">{user.username}</Field>
            <Field label="Email">{user.email}</Field>
            <Field label="First Name">{user.first_name ?? '—'}</Field>
            <Field label="Last Name">{user.last_name ?? '—'}</Field>
            <Field label="Status">
              <StatusBadge status={user.status} />
            </Field>
            <Field label="Created">{formatDate(user.created_at)}</Field>
            {user.last_login_at && <Field label="Last Login">{formatDate(user.last_login_at)}</Field>}
          </dl>
        </TabsContent>

        <TabsContent value="roles">
          <div className="flex flex-col gap-2">
            {user.roles.map((role) => (
              <Badge key={role} variant="primary">
                {role}
              </Badge>
            ))}
            {user.roles.length === 0 && (
              <EmptyState title="No roles assigned" description="This user has no assigned roles." />
            )}
          </div>
        </TabsContent>

        <TabsContent value="permissions">
          <div className="flex flex-col gap-2">
            {user.effective_permissions.map((slug) => (
              <span key={slug} className="font-mono text-body text-neutral-800">
                {slug}
              </span>
            ))}
            {user.effective_permissions.length === 0 && (
              <EmptyState
                title="No effective permissions"
                description="This user currently has no permissions through assigned roles."
              />
            )}
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}
