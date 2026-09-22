import { ConfirmDialog } from '@multistack/ui';
import { ApiError } from '../../api/client';
import type { Organization } from '../../api/types';
import { useToast } from '../../app/ToastProvider';
import { useSuspendOrganization } from './hooks/useSuspendOrganization';

export interface SuspendOrganizationDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  organization: Organization;
}

/** Confirmation wording matches actual backend semantics (§28, §146) — no invented lifecycle rules. */
export function SuspendOrganizationDialog({ open, onOpenChange, organization }: SuspendOrganizationDialogProps) {
  const suspend = useSuspendOrganization(organization.id);
  const toast = useToast();

  async function handleConfirm() {
    try {
      await suspend.mutateAsync();
      onOpenChange(false);
      toast.success('Organization suspended', `${organization.name} has been suspended.`);
    } catch (err) {
      toast.error('Unable to suspend organization', err instanceof ApiError ? err.message : undefined);
    }
  }

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Suspend ${organization.name}?`}
      description="Users may no longer be able to access organization resources."
      confirmLabel="Suspend"
      destructive
      loading={suspend.isPending}
      onConfirm={handleConfirm}
    />
  );
}
