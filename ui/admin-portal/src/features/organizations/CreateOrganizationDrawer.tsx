import { zodResolver } from '@hookform/resolvers/zod';
import { EntityDrawer, FormField, Input, Select } from '@multistack/ui';
import { Controller, useForm } from 'react-hook-form';
import { useLocation, useNavigate } from 'react-router-dom';
import { z } from 'zod';
import { ApiError } from '../../api/client';
import type { Plan } from '../../api/types';
import { useToast } from '../../app/ToastProvider';
import { useCreateOrganization } from './hooks/useCreateOrganization';

const schema = z.object({
  name: z.string().min(1, 'Organization name is required.'),
  plan_id: z.string().min(1, 'Plan is required.'),
});
type FormValues = z.infer<typeof schema>;

export interface CreateOrganizationDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  plans: Plan[];
}

export function CreateOrganizationDrawer({ open, onOpenChange, plans }: CreateOrganizationDrawerProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const toast = useToast();
  const createOrganization = useCreateOrganization();
  const {
    register,
    handleSubmit,
    control,
    setError,
    reset,
    formState: { errors, isDirty },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { name: '', plan_id: '' },
  });

  const onSubmit = handleSubmit(async (values) => {
    try {
      const created = await createOrganization.mutateAsync(values);
      reset();
      onOpenChange(false);
      toast.success('Organization created', `${created.name} is now provisioning.`);
      navigate(`/organizations/${created.id}`, { state: { backgroundLocation: location } });
    } catch (err) {
      if (err instanceof ApiError && err.field) {
        setError(err.field as keyof FormValues, { message: err.message });
      }
      toast.error('Unable to create organization', err instanceof ApiError ? err.message : undefined);
    }
  });

  return (
    <EntityDrawer
      open={open}
      onOpenChange={onOpenChange}
      title="Create Organization"
      dirty={isDirty}
      submitting={createOrganization.isPending}
      onSubmit={onSubmit}
      submitLabel="Create Organization"
    >
      <form className="flex flex-col gap-4" onSubmit={onSubmit}>
        <FormField label="Organization name" htmlFor="org-name" error={errors.name?.message} required>
          <Input {...register('name')} />
        </FormField>
        <FormField label="Plan" htmlFor="org-plan" error={errors.plan_id?.message} required>
          {/* FormField clones its direct child to inject id/aria-* — but
              Controller doesn't forward unknown props into its render prop,
              so those must be passed explicitly here instead. */}
          <Controller
            control={control}
            name="plan_id"
            render={({ field }) => (
              <Select
                id="org-plan"
                aria-invalid={errors.plan_id ? true : undefined}
                value={field.value}
                onValueChange={field.onChange}
                options={plans.map((p) => ({ value: p.id, label: p.name }))}
                placeholder="Select a plan"
              />
            )}
          />
        </FormField>
      </form>
    </EntityDrawer>
  );
}
