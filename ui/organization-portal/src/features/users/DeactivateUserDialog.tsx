import { ConfirmDialog } from '@multistack/ui';
import { ApiError } from '../../api/client';
import type { User } from '../../api/types';
import { useToast } from '../../app/ToastProvider';
import { useUpdateUser } from './hooks/useUpdateUser';

export interface DeactivateUserDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  user: User;
}

/** Soft, reversible status change (PATCH status: 'suspended') — distinct from DeleteUserDialog's
 * permanent removal. A suspended user can be reactivated later from the same row menu (no
 * confirmation needed for that direction — reactivating isn't destructive). */
export function DeactivateUserDialog({ open, onOpenChange, user }: DeactivateUserDialogProps) {
  const updateUser = useUpdateUser(user.id);
  const toast = useToast();

  async function handleConfirm() {
    try {
      await updateUser.mutateAsync({ status: 'suspended' });
      onOpenChange(false);
      toast.success('User deactivated', `${user.display_name ?? user.username} has been deactivated.`);
    } catch (err) {
      toast.error('Unable to deactivate user', err instanceof ApiError ? err.message : undefined);
    }
  }

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Deactivate ${user.display_name ?? user.username}?`}
      description="They will no longer be able to sign in. This can be undone later by reactivating the user."
      confirmLabel="Deactivate"
      destructive
      loading={updateUser.isPending}
      onConfirm={handleConfirm}
    />
  );
}
