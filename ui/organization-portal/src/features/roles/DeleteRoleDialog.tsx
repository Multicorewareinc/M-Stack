import { ConfirmDialog } from '@multistack/ui';
import * as React from 'react';
import { ApiError } from '../../api/client';
import type { Role } from '../../api/types';
import { useToast } from '../../app/ToastProvider';
import { useDeleteRole } from './hooks/useDeleteRole';

export interface DeleteRoleDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  role: Role;
}

/**
 * If the backend rejects deletion because the role is assigned to users,
 * that message is surfaced in place rather than assuming any client-side
 * dependency rule (§112).
 */
export function DeleteRoleDialog({ open, onOpenChange, role }: DeleteRoleDialogProps) {
  const deleteRole = useDeleteRole();
  const [conflictMessage, setConflictMessage] = React.useState<string | null>(null);
  const toast = useToast();

  async function handleConfirm() {
    setConflictMessage(null);
    try {
      await deleteRole.mutateAsync(role.id);
      onOpenChange(false);
      toast.success('Role deleted', role.name);
    } catch (err) {
      if (err instanceof ApiError && err.code === 'CONFLICT') {
        setConflictMessage(err.message);
      } else {
        toast.error('Unable to delete role', err instanceof ApiError ? err.message : undefined);
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
      title={`Delete role "${role.name}"?`}
      description={conflictMessage ?? 'This action cannot be undone.'}
      confirmLabel="Delete"
      destructive
      loading={deleteRole.isPending}
      onConfirm={handleConfirm}
    />
  );
}
