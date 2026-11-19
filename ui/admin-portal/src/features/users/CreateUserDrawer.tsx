import { zodResolver } from '@hookform/resolvers/zod';
import { EntityDrawer, FormField, Input, Select } from '@multistack/ui';
import { Controller, useForm } from 'react-hook-form';
import { z } from 'zod';
import { ApiError } from '../../api/client';
import type { Organization } from '../../api/types';
import { useToast } from '../../app/ToastProvider';
import { useCreateUser } from './hooks/useCreateUser';

// Matches organization-control-plane's password policy (settings.password_min_length).
const schema = z.object({
  organization_id: z.string().min(1, 'Organization is required.'),
  username: z.string().min(1, 'Username is required.'),
  email: z.string().min(1, 'Email is required.').email('Enter a valid email address.'),
  display_name: z.string().optional(),
  password: z.string().min(12, 'Temporary password must be at least 12 characters.'),
});
type FormValues = z.infer<typeof schema>;

const EMPTY_VALUES: FormValues = {
  organization_id: '',
  username: '',
  email: '',
  display_name: '',
  password: '',
};

export interface CreateUserDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  organizations: Organization[];
}

/**
 * Super-admin bootstraps the FIRST login-capable user for an org (POST
 * /v1/organizations/{org_id}/users). The backend always sets must_change_password=true for a
 * password-provisioned user (mirrors seed_admin), so on their first org-portal login they're
 * forced through the reset-password flow before landing in the app — no separate toggle needed
 * here for that behavior.
 */
export function CreateUserDrawer({ open, onOpenChange, organizations }: CreateUserDrawerProps) {
  const createUser = useCreateUser();
  const toast = useToast();

  const {
    register,
    handleSubmit,
    control,
    setError,
    reset,
    formState: { errors, isDirty },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: EMPTY_VALUES,
  });

  const onSubmit = handleSubmit(async ({ organization_id, ...input }) => {
    try {
      const created = await createUser.mutateAsync({ orgId: organization_id, input });
      reset(EMPTY_VALUES);
      onOpenChange(false);
      toast.success('User created', `${created.username} must reset their password on first login.`);
    } catch (err) {
      if (err instanceof ApiError && err.field) {
        setError(err.field as keyof FormValues, { message: err.message });
      } else {
        toast.error('Unable to create user', err instanceof ApiError ? err.message : undefined);
      }
    }
  });

  return (
    <EntityDrawer
      open={open}
      onOpenChange={(next) => {
        if (!next) reset(EMPTY_VALUES);
        onOpenChange(next);
      }}
      title="Create User"
      dirty={isDirty}
      submitting={createUser.isPending}
      onSubmit={onSubmit}
      submitLabel="Create User"
    >
      <form className="flex flex-col gap-4" onSubmit={onSubmit}>
        <FormField label="Organization" htmlFor="user-org" error={errors.organization_id?.message} required>
          {/* FormField clones its direct child to inject id/aria-* — but Controller doesn't
              forward unknown props into its render prop, so those must be passed explicitly here. */}
          <Controller
            control={control}
            name="organization_id"
            render={({ field }) => (
              <Select
                id="user-org"
                aria-invalid={errors.organization_id ? true : undefined}
                value={field.value}
                onValueChange={field.onChange}
                options={organizations.map((o) => ({ value: o.id, label: o.name }))}
                placeholder="Select an organization"
              />
            )}
          />
        </FormField>
        <FormField label="Username" htmlFor="user-username" error={errors.username?.message} required>
          <Input id="user-username" {...register('username')} />
        </FormField>
        <FormField label="Email" htmlFor="user-email" error={errors.email?.message} required>
          <Input id="user-email" type="email" {...register('email')} />
        </FormField>
        <FormField label="Display name" htmlFor="user-display-name" error={errors.display_name?.message}>
          <Input id="user-display-name" {...register('display_name')} />
        </FormField>
        <FormField
          label="Temporary password"
          htmlFor="user-password"
          error={errors.password?.message}
          required
          hint="The user must reset this on their first login."
        >
          <Input id="user-password" type="password" autoComplete="new-password" {...register('password')} />
        </FormField>
      </form>
    </EntityDrawer>
  );
}
