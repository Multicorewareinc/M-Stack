import { Alert, EntityDrawer, FormField, Select } from '@multistack/ui';
import * as React from 'react';
import { ApiError } from '../../api/client';
import type { Organization, Plan } from '../../api/types';
import { useToast } from '../../app/ToastProvider';
import { useChangePlan } from './hooks/useChangePlan';

export interface ChangePlanDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  organization: Organization;
  plans: Plan[];
}

export function ChangePlanDrawer({ open, onOpenChange, organization, plans }: ChangePlanDrawerProps) {
  const [selectedPlanId, setSelectedPlanId] = React.useState(organization.plan_id);
  const changePlan = useChangePlan(organization.id);
  const toast = useToast();

  const currentPlan = plans.find((p) => p.id === organization.plan_id);
  const newPlan = plans.find((p) => p.id === selectedPlanId);
  const isChanged = selectedPlanId !== organization.plan_id;

  async function handleSubmit() {
    try {
      await changePlan.mutateAsync(selectedPlanId);
      onOpenChange(false);
      toast.success('Plan updated', `${organization.name} is now on ${newPlan?.name ?? 'the new plan'}.`);
    } catch (err) {
      toast.error('Unable to change plan', err instanceof ApiError ? err.message : undefined);
    }
  }

  return (
    <EntityDrawer
      open={open}
      onOpenChange={onOpenChange}
      title="Change Plan"
      dirty={isChanged}
      submitting={changePlan.isPending}
      onSubmit={handleSubmit}
      submitLabel="Update Plan"
    >
      <div className="flex flex-col gap-4">
        <div className="flex flex-col gap-1">
          <span className="text-secondary font-medium text-neutral-700">Current plan</span>
          <span data-testid="current-plan-value" className="text-body text-neutral-900">
            {currentPlan?.name ?? organization.plan_id}
          </span>
        </div>
        <FormField label="New plan" htmlFor="new-plan">
          <Select
            value={selectedPlanId}
            onValueChange={setSelectedPlanId}
            options={plans.map((p) => ({ value: p.id, label: p.name }))}
          />
        </FormField>
        <Alert variant="warning" data-testid="plan-change-summary">
          This changes the organization&apos;s platform entitlement.
          {isChanged && newPlan && (
            <>
              {' '}
              Current: <strong>{currentPlan?.name ?? organization.plan_id}</strong>. New: <strong>{newPlan.name}</strong>.
            </>
          )}
        </Alert>
      </div>
    </EntityDrawer>
  );
}
