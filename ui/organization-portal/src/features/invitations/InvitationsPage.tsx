import { Button, ConfirmDialog, DataTable, FormField, Input, PageHeader, Select, type Column } from '@multistack/ui';
import * as React from 'react';
import { ApiError } from '../../api/client';
import type { Invitation, InvitationListItem } from '../../api/invitations';
import { useToast } from '../../app/ToastProvider';
import { formatDate } from '../../lib/formatters';
import { useRolesForSelect } from '../users/hooks/useRolesForSelect';
import { formatExpiry, isExpired } from './expiry';
import { INVITATIONS_PAGE_SIZE, useInvitations, useRevokeInvitation } from './hooks/useInvitations';
import { InviteLinkCard } from './InviteLinkCard';
import { InviteMemberDialog } from './InviteMemberDialog';

const ALL = 'all';

/**
 * Invite people into this organization and manage the invitations still waiting to be accepted. Backed
 * by modules/invitations on organization-control-plane (JWT surface, scoped to the signed-in user's own
 * organization server-side).
 */
export function InvitationsPage() {
  const toast = useToast();
  const [inviteOpen, setInviteOpen] = React.useState(false);
  const [issued, setIssued] = React.useState<{ invitation: Invitation; link: string } | null>(null);
  const [emailInput, setEmailInput] = React.useState('');
  const [search, setSearch] = React.useState('');
  const [role, setRole] = React.useState(ALL);
  const [offset, setOffset] = React.useState(0);
  const [revokeTarget, setRevokeTarget] = React.useState<InvitationListItem | null>(null);

  const { data, isLoading, error, refetch, isFetching } = useInvitations(
    { search: search || undefined, role: role === ALL ? undefined : role },
    offset,
  );
  const { data: roles } = useRolesForSelect();
  const revoke = useRevokeInvitation();

  function applySearch(event: React.FormEvent) {
    event.preventDefault();
    setSearch(emailInput.trim());
    setOffset(0);
  }

  async function confirmRevoke() {
    if (!revokeTarget) return;
    try {
      await revoke.mutateAsync(revokeTarget.id);
      toast.success('Invitation revoked', `${revokeTarget.email}'s link no longer works.`);
    } catch (err) {
      toast.error('Unable to revoke invitation', err instanceof ApiError ? err.message : undefined);
    } finally {
      setRevokeTarget(null); // close either way, so the result (and the list) is visible
    }
  }

  const columns: Column<InvitationListItem>[] = [
    { key: 'email', header: 'Email', accessor: (i) => i.email },
    { key: 'role', header: 'Role', accessor: (i) => i.role_name },
    { key: 'created', header: 'Created', accessor: (i) => formatDate(i.created_at) },
    {
      key: 'expires',
      header: 'Expires',
      render: (i) => (
        <span className={isExpired(i.expires_at) ? 'text-danger-700' : undefined}>{formatExpiry(i.expires_at)}</span>
      ),
    },
    {
      key: 'actions',
      header: 'Actions',
      className: 'text-right',
      render: (i) => (
        <Button
          variant="ghost"
          size="sm"
          aria-label={`Revoke invitation for ${i.email}`}
          onClick={() => setRevokeTarget(i)}
        >
          Revoke
        </Button>
      ),
    },
  ];

  const total = data?.total ?? 0;
  const first = total === 0 ? 0 : offset + 1;
  const last = Math.min(offset + INVITATIONS_PAGE_SIZE, total);

  return (
    <div>
      <PageHeader
        title="Invitations"
        description="Invite people to your organization and manage pending invitations."
        actions={<Button onClick={() => setInviteOpen(true)}>Invite member</Button>}
      />

      {issued && (
        <InviteLinkCard
          link={issued.link}
          email={issued.invitation.email}
          role={issued.invitation.role_name}
          emailStatus={
            issued.invitation.email_status === 'sent' ? undefined : issued.invitation.email_status
          }
          emailDetail={issued.invitation.email_detail}
          onDismiss={() => setIssued(null)}
        />
      )}

      <form
        onSubmit={applySearch}
        aria-label="Search invitations"
        className="mb-4 flex flex-col gap-3 rounded-lg border border-subtle bg-neutral-0 p-4"
      >
        <h2 className="text-body font-semibold text-neutral-900">Search invitations</h2>
        <div className="flex flex-wrap items-end gap-3">
          <FormField label="Email" htmlFor="invitations-email" className="w-64">
            <Input
              id="invitations-email"
              type="search"
              placeholder="Email"
              value={emailInput}
              onChange={(e) => setEmailInput(e.target.value)}
            />
          </FormField>
          <FormField label="Role" htmlFor="invitations-role" className="w-52">
            <Select
              id="invitations-role"
              value={role}
              onValueChange={(value) => {
                setRole(value);
                setOffset(0);
              }}
              options={[
                { value: ALL, label: 'All roles' },
                ...(roles ?? []).map((r) => ({ value: r.name, label: r.name })),
              ]}
            />
          </FormField>
          <Button type="submit">Search</Button>
        </div>
      </form>

      <DataTable
        columns={columns}
        rows={data?.items ?? []}
        getRowId={(i) => i.id}
        loading={isLoading}
        error={error ? error.message : null}
        onRetry={() => refetch()}
        empty={{
          title: 'No pending invitations',
          description: search || role !== ALL ? 'Nothing matches these filters.' : 'Invite someone to get started.',
        }}
      />
      <div className="mt-3 flex items-center justify-between text-body text-neutral-600">
        <span aria-live="polite">{total === 0 ? 'No invitations' : `Showing ${first}–${last} of ${total}`}</span>
        <div className="flex gap-2">
          <Button
            variant="secondary"
            disabled={offset === 0 || isFetching}
            onClick={() => setOffset(Math.max(0, offset - INVITATIONS_PAGE_SIZE))}
          >
            Previous
          </Button>
          <Button
            variant="secondary"
            disabled={offset + INVITATIONS_PAGE_SIZE >= total || isFetching}
            onClick={() => setOffset(offset + INVITATIONS_PAGE_SIZE)}
          >
            Next
          </Button>
        </div>
      </div>

      {inviteOpen && (
        <InviteMemberDialog
          open={inviteOpen}
          onOpenChange={setInviteOpen}
          onCreated={(invitation, link) => {
            setOffset(0);
            if (invitation.email_status === 'sent') {
              // The invitee has the link in their inbox, so there is nothing for the admin to copy.
              setIssued(null);
              toast.success('Invitation sent', `We emailed the invitation to ${invitation.email}.`);
            } else {
              // Not emailed (not configured, or it failed): the link is shown once and cannot be fetched
              // again, so keep it on screen for the admin to send by hand.
              setIssued({ invitation, link });
            }
          }}
        />
      )}
      <ConfirmDialog
        open={revokeTarget !== null}
        onOpenChange={(open) => !open && setRevokeTarget(null)}
        title="Revoke invitation?"
        description={
          revokeTarget ? `${revokeTarget.email} will no longer be able to use their invitation link.` : undefined
        }
        confirmLabel="Revoke"
        destructive
        loading={revoke.isPending}
        onConfirm={confirmRevoke}
      />
    </div>
  );
}
