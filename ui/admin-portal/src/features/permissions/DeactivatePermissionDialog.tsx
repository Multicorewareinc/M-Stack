import { ConfirmDialog } from '@multistack/ui';
import * as React from 'react';
import { ApiError } from '../../api/client';
import type { Permission } from '../../api/types';
import { useToast } from '../../app/ToastProvider';
import { useDeactivatePermission } from './hooks/useDeactivatePermission';

export interface DeactivatePermissionDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  permission: Permission;
}

/**
 * Always a soft-deactivate (PATCH is_active:false) — never a delete (§52).
 * If the backend rejects deactivation because the permission is referenced
 * by one or more roles, that message is surfaced in place rather than
 * assuming any client-side dependency rule.
 */
export function DeactivatePermissionDialog({ open, onOpenChange, permission }: DeactivatePermissionDialogProps) {
  const deactivatePermission = useDeactivatePermission();
  const [conflictMessage, setConflictMessage] = React.useState<string | null>(null);
  const toast = useToast();

  async function handleConfirm() {
    setConflictMessage(null);
    try {
      await deactivatePermission.mutateAsync(permission.id);
      onOpenChange(false);
      toast.success('Permission deactivated', permission.slug);
    } catch (err) {
      if (err instanceof ApiError && err.code === 'CONFLICT') {
        setConflictMessage(err.message);
      } else {
        toast.error('Unable to deactivate permission', err instanceof ApiError ? err.message : undefined);
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
      title={`Deactivate ${permission.slug}?`}
      description={conflictMessage ?? 'This permission will no longer be available for new role composition.'}
      confirmLabel="Deactivate"
      destructive
      loading={deactivatePermission.isPending}
      onConfirm={handleConfirm}
    />
  );
}
