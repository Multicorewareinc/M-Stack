import { useQuery } from '@tanstack/react-query';
import { EmptyState, PageHeader, StatusBadge, Tabs, TabsContent, TabsList, TabsTrigger } from '@multistack/ui';
import * as React from 'react';
import { useParams } from 'react-router-dom';
import { getOrganizationUsers } from '../../api/organizations';
import type { UserDirectoryEntry } from '../../api/types';
import { formatDate } from '../../lib/formatters';
import { UserDirectoryTable } from '../users/UserDirectoryTable';
import { useProvisioningStatus } from './hooks/useProvisioningStatus';
import { usePlansForSelect } from './hooks/usePlansForSelect';

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-secondary text-neutral-500">{label}</dt>
      <dd className="text-body text-neutral-900">{children}</dd>
    </div>
  );
}

export function OrganizationDetailPage() {
  const { id = '' } = useParams();
  // Polls while provisioning and stops automatically at a terminal state
  // (active/failed) — the detail page reflects the transition live, without
  // a manual reload (§37, §91).
  const { data: org, isLoading, isError } = useProvisioningStatus(id);
  const { data: plans } = usePlansForSelect();
  const { data: users } = useQuery({
    queryKey: ['organization-users', id],
    queryFn: () => getOrganizationUsers(id) as Promise<UserDirectoryEntry[]>,
    enabled: Boolean(org),
  });

  if (isLoading) {
    return <div data-testid="org-detail-loading">Loading organization…</div>;
  }

  if (isError || !org) {
    return (
      <EmptyState
        title="Organization not found"
        description="The organization may have been deleted or you may no longer have access."
      />
    );
  }

  const planName = plans?.find((p) => p.id === org.plan_id)?.name ?? org.plan_id;

  return (
    <div>
      <PageHeader eyebrow="Platform Control Plane / Organizations" title={org.name} />
      <div className="mb-4 flex items-center gap-2">
        <StatusBadge status={org.status} />
        <span className="text-secondary text-neutral-500">{planName}</span>
      </div>

      <Tabs defaultValue="overview">
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="users">Users</TabsTrigger>
          <TabsTrigger value="configuration">Configuration</TabsTrigger>
        </TabsList>

        <TabsContent value="overview">
          <dl className="grid grid-cols-2 gap-4">
            <Field label="Organization ID">{org.id}</Field>
            <Field label="Plan">{planName}</Field>
            <Field label="Status">
              <StatusBadge status={org.status} />
            </Field>
            <Field label="Users">{org.user_count}</Field>
            <Field label="Created">{formatDate(org.created_at)}</Field>
            <Field label="Updated">{formatDate(org.updated_at)}</Field>
          </dl>
        </TabsContent>

        <TabsContent value="users">
          {/* Shared with the platform Users page (SP-06) so the two
              directory-list contexts never diverge. */}
          <UserDirectoryTable rows={users ?? []} organizations={org ? [{ id: org.id, name: org.name }] : []} />
        </TabsContent>

        <TabsContent value="configuration">
          {/* Only fields the Admin CP contract actually returns (§105) — no
              internal infrastructure info. */}
          <dl className="grid grid-cols-2 gap-4">
            <Field label="Name">{org.name}</Field>
            <Field label="Slug">
              <span className="font-mono">{org.slug}</span>
            </Field>
            <Field label="Status">
              <StatusBadge status={org.status} />
            </Field>
            <Field label="Plan">{planName}</Field>
            <Field label="Organization ID">
              <span className="font-mono">{org.id}</span>
            </Field>
            <Field label="Created At">{formatDate(org.created_at)}</Field>
            <Field label="Updated At">{formatDate(org.updated_at)}</Field>
          </dl>
        </TabsContent>
      </Tabs>
    </div>
  );
}
