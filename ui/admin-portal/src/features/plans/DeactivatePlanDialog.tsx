import { ConfirmDialog } from '@multistack/ui';
import { ApiError } from '../../api/client';
import type { Plan } from '../../api/types';
import { useToast } from '../../app/ToastProvider';
import { useDeactivatePlan } from './hooks/useDeactivatePlan';

export interface DeactivatePlanDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  plan: Plan;
}

/** Always a soft-deactivate (PATCH is_active:false) — never a delete (§47). */
export function DeactivatePlanDialog({ open, onOpenChange, plan }: DeactivatePlanDialogProps) {
  const deactivatePlan = useDeactivatePlan();
  const toast = useToast();

  async function handleConfirm() {
    try {
      await deactivatePlan.mutateAsync(plan.id);
      onOpenChange(false);
      toast.success('Plan deactivated', plan.name);
    } catch (err) {
      toast.error('Unable to deactivate plan', err instanceof ApiError ? err.message : undefined);
    }
  }

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Deactivate Plan?"
      description="Existing organizations using this plan may remain associated with it. New assignments will be blocked."
      confirmLabel="Deactivate"
      destructive
      loading={deactivatePlan.isPending}
      onConfirm={handleConfirm}
    />
  );
}
