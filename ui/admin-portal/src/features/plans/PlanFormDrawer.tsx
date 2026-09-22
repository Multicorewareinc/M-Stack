import { zodResolver } from '@hookform/resolvers/zod';
import { EntityDrawer, FormField, Input } from '@multistack/ui';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { ApiError } from '../../api/client';
import type { Plan } from '../../api/types';
import { useToast } from '../../app/ToastProvider';
import { useCreatePlan } from './hooks/useCreatePlan';
import { useUpdatePlan } from './hooks/useUpdatePlan';

const schema = z.object({
  name: z.string().min(1, 'Plan name is required.'),
  tpm: z.coerce.number().int('Must be a whole number.').nonnegative('Must not be negative.'),
  rpm: z.coerce.number().int('Must be a whole number.').nonnegative('Must not be negative.'),
  quota_monthly_tokens: z.coerce.number().int('Must be a whole number.').nonnegative('Must not be negative.'),
});
type FormValues = z.infer<typeof schema>;

const EMPTY_VALUES: FormValues = { name: '', tpm: 0, rpm: 0, quota_monthly_tokens: 0 };

export interface PlanFormDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Present -> edit mode, pre-filled with this plan's current values. Absent -> create mode. */
  plan?: Plan;
}

export function PlanFormDrawer({ open, onOpenChange, plan }: PlanFormDrawerProps) {
  const isEdit = Boolean(plan);
  const createPlan = useCreatePlan();
  const updatePlan = useUpdatePlan(plan?.id ?? '');
  const toast = useToast();

  const {
    register,
    handleSubmit,
    setError,
    reset,
    formState: { errors, isDirty },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    // `values` (not `defaultValues`) keeps the form in sync whenever the
    // `plan` prop changes — pre-fills edit mode without a manual reset effect.
    values: plan
      ? { name: plan.name, tpm: plan.tpm, rpm: plan.rpm, quota_monthly_tokens: plan.quota_monthly_tokens }
      : EMPTY_VALUES,
  });

  const mutation = isEdit ? updatePlan : createPlan;

  const onSubmit = handleSubmit(async (values) => {
    try {
      if (isEdit) {
        await updatePlan.mutateAsync(values);
      } else {
        await createPlan.mutateAsync(values);
      }
      reset(EMPTY_VALUES);
      onOpenChange(false);
      toast.success(isEdit ? 'Plan updated' : 'Plan created', values.name);
    } catch (err) {
      if (err instanceof ApiError && err.field) {
        setError(err.field as keyof FormValues, { message: err.message });
      } else {
        toast.error(isEdit ? 'Unable to update plan' : 'Unable to create plan', err instanceof ApiError ? err.message : undefined);
      }
    }
  });

  return (
    <EntityDrawer
      open={open}
      onOpenChange={onOpenChange}
      title={isEdit ? 'Edit Plan' : 'Create Plan'}
      dirty={isDirty}
      submitting={mutation.isPending}
      onSubmit={onSubmit}
      submitLabel={isEdit ? 'Save Changes' : 'Create Plan'}
    >
      <form className="flex flex-col gap-4" onSubmit={onSubmit}>
        <FormField label="Name" htmlFor="plan-name" error={errors.name?.message} required>
          <Input {...register('name')} />
        </FormField>
        <FormField label="TPM Limit" htmlFor="plan-tpm" error={errors.tpm?.message} required>
          <Input type="number" {...register('tpm')} />
        </FormField>
        <FormField label="RPM Limit" htmlFor="plan-rpm" error={errors.rpm?.message} required>
          <Input type="number" {...register('rpm')} />
        </FormField>
        <FormField label="Quota" htmlFor="plan-quota" error={errors.quota_monthly_tokens?.message} required>
          <Input type="number" {...register('quota_monthly_tokens')} />
        </FormField>
      </form>
    </EntityDrawer>
  );
}
