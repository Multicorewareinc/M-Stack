import { ConfirmDialog } from '@multistack/ui';
import * as React from 'react';
import { ApiError } from '../../api/client';
import type { Organization } from '../../api/types';
import { useToast } from '../../app/ToastProvider';
import { useDeleteOrganization } from './hooks/useDeleteOrganization';

export interface DeleteOrganizationDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  organization: Organization;
}

/**
 * Hard delete (DELETE /organizations/{id}) — distinct from Suspend, which is reversible. If the
 * backend rejects deletion because dependent records still exist, that message is surfaced in
 * place rather than assuming any client-side dependency rule.
 */
export function DeleteOrganizationDialog({ open, onOpenChange, organization }: DeleteOrganizationDialogProps) {
  const deleteOrganization = useDeleteOrganization();
  const [conflictMessage, setConflictMessage] = React.useState<string | null>(null);
  const toast = useToast();

  async function handleConfirm() {
    setConflictMessage(null);
    try {
      await deleteOrganization.mutateAsync(organization.id);
      onOpenChange(false);
      toast.success('Organization deleted', organization.name);
    } catch (err) {
      if (err instanceof ApiError && err.code === 'CONFLICT') {
        setConflictMessage(err.message);
      } else {
        toast.error('Unable to delete organization', err instanceof ApiError ? err.message : undefined);
      }
    }
  }

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={(next) => {
        if (!next) setConflictMessage(null);
        onOpenChange(next);
      }}
      title={`Delete ${organization.name}?`}
      description={conflictMessage ?? 'This permanently removes the organization. This action cannot be undone.'}
      confirmLabel="Delete"
      destructive
      loading={deleteOrganization.isPending}
      onConfirm={handleConfirm}
    />
  );
}
