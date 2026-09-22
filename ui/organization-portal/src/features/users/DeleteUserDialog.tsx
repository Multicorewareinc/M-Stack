import { ConfirmDialog } from '@multistack/ui';
import * as React from 'react';
import { ApiError } from '../../api/client';
import type { User } from '../../api/types';
import { useToast } from '../../app/ToastProvider';
import { useDeleteUser } from './hooks/useDeleteUser';

export interface DeleteUserDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  user: User;
}

/**
 * Hard delete (DELETE /users/{id}). If the backend rejects deletion because dependent records
 * still exist, that message is surfaced in place rather than assuming any client-side dependency
 * rule — same pattern as DeleteRoleDialog.
 */
export function DeleteUserDialog({ open, onOpenChange, user }: DeleteUserDialogProps) {
  const deleteUser = useDeleteUser();
  const [conflictMessage, setConflictMessage] = React.useState<string | null>(null);
  const toast = useToast();

  async function handleConfirm() {
    setConflictMessage(null);
    try {
      await deleteUser.mutateAsync(user.id);
      onOpenChange(false);
      toast.success('User deleted', user.display_name ?? user.username);
    } catch (err) {
      if (err instanceof ApiError && err.code === 'CONFLICT') {
        setConflictMessage(err.message);
      } else {
        toast.error('Unable to delete user', err instanceof ApiError ? err.message : undefined);
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
      title={`Delete ${user.display_name ?? user.username}?`}
      description={conflictMessage ?? 'This permanently removes the user. This action cannot be undone.'}
      confirmLabel="Delete"
      destructive
      loading={deleteUser.isPending}
      onConfirm={handleConfirm}
    />
  );
}
