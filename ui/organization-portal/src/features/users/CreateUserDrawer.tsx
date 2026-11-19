import { zodResolver } from '@hookform/resolvers/zod';
import { EntityDrawer, FormField, Input, Select } from '@multistack/ui';
import { useQueryClient } from '@tanstack/react-query';
import * as React from 'react';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { ApiError } from '../../api/client';
import { queryKeys } from '../../api/queryKeys';
import { setUserRoles } from '../../api/users';
import { useToast } from '../../app/ToastProvider';
import { usePermission } from '../../hooks/usePermission';
import { useCreateUser } from './hooks/useCreateUser';
import { useOrgId } from './hooks/useOrgId';
import { useRolesForSelect } from './hooks/useRolesForSelect';

// password min-length matches organization-control-plane's settings.password_min_length.
const schema = z.object({
  username: z.string().min(1, 'Username is required.'),
  email: z.string().min(1, 'Email is required.').email('Enter a valid email address.'),
  first_name: z.string().optional(),
  last_name: z.string().optional(),
  display_name: z.string().optional(),
  password: z.string().min(12, 'Temporary password must be at least 12 characters.'),
});
type FormValues = z.infer<typeof schema>;

const EMPTY_VALUES: FormValues = {
  username: '',
  email: '',
  first_name: '',
  last_name: '',
  display_name: '',
  password: '',
};

export interface CreateUserDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

// No status field: new users always start `active` server-side (UserCreate has no status field
// at all; only UserUpdate does).
//
// Role selection IS offered here, deliberately overriding the older "role assignment is always a
// separate operation, never bundled into create" stance (§60/§57): POST /v1/users (unlike the
// newer identity-gated POST /api/users) assigns no default role at all, so a user created from
// this drawer was landing role-less until someone remembered to assign one from their detail
// page afterward. Selecting a role right here closes that gap; the Roles tab on UserDetailPage
// still exists for changing it later.
//
// Single-select by product rule: a user gets exactly one role, not several — a dropdown, never a
// multi-select control.
export function CreateUserDrawer({ open, onOpenChange }: CreateUserDrawerProps) {
  const createUser = useCreateUser();
  const canCreate = usePermission('users.create');
  const toast = useToast();
  const queryClient = useQueryClient();
  const orgId = useOrgId();
  const { data: roles } = useRolesForSelect();

  const [selectedRoleId, setSelectedRoleId] = React.useState('');
  const [savingRoles, setSavingRoles] = React.useState(false);

  // A closed-without-saving attempt shouldn't leave the role pick armed for next time the drawer
  // opens — this state lives outside react-hook-form's own reset(), which only covers the text
  // fields.
  React.useEffect(() => {
    if (open) setSelectedRoleId('');
  }, [open]);

  const {
    register,
    handleSubmit,
    setError,
    reset,
    formState: { errors, isDirty },
  } = useForm<FormValues>({ resolver: zodResolver(schema), defaultValues: EMPTY_VALUES });

  const onSubmit = handleSubmit(async (values) => {
    try {
      const created = await createUser.mutateAsync(values);
      if (selectedRoleId) {
        setSavingRoles(true);
        try {
          await setUserRoles(created.id, [selectedRoleId]);
          queryClient.invalidateQueries({ queryKey: queryKeys.userRoles(orgId, created.id) });
          queryClient.invalidateQueries({ queryKey: queryKeys.user(orgId, created.id) });
        } catch (err) {
          // The user itself was created successfully — only the role attach failed. Say what
          // actually happened rather than implying the whole operation failed (same recovery
          // posture as RoleFormDrawer's permission-attach failure).
          toast.error(
            'User created, but roles could not be assigned',
            err instanceof ApiError ? `${err.message} — assign them from Manage Roles.` : undefined,
          );
          reset(EMPTY_VALUES);
          onOpenChange(false);
          return;
        } finally {
          setSavingRoles(false);
        }
      }
      reset(EMPTY_VALUES);
      onOpenChange(false);
      toast.success('User created', `${values.username} must reset their password on first login.`);
    } catch (err) {
      if (err instanceof ApiError && err.field) {
        setError(err.field as keyof FormValues, { message: err.message });
      } else {
        toast.error('Unable to create user', err instanceof ApiError ? err.message : undefined);
      }
    }
  });

  if (!canCreate) return null;

  return (
    <EntityDrawer
      open={open}
      onOpenChange={onOpenChange}
      title="Create User"
      dirty={isDirty || Boolean(selectedRoleId)}
      submitting={createUser.isPending || savingRoles}
      onSubmit={onSubmit}
      submitLabel="Create User"
    >
      <form className="flex flex-col gap-4" onSubmit={onSubmit}>
        <FormField label="Username" htmlFor="user-username" error={errors.username?.message} required>
          <Input {...register('username')} />
        </FormField>
        <FormField label="Email" htmlFor="user-email" error={errors.email?.message} required>
          <Input {...register('email')} />
        </FormField>
        <FormField label="First Name" htmlFor="user-first-name">
          <Input {...register('first_name')} />
        </FormField>
        <FormField label="Last Name" htmlFor="user-last-name">
          <Input {...register('last_name')} />
        </FormField>
        <FormField label="Display Name" htmlFor="user-display-name">
          <Input {...register('display_name')} />
        </FormField>
        <FormField
          label="Temporary Password"
          htmlFor="user-password"
          error={errors.password?.message}
          required
          hint="The user must reset this on their first login."
        >
          <Input id="user-password" type="password" autoComplete="new-password" {...register('password')} />
        </FormField>

        <div className="flex flex-col gap-2 border-t border-subtle pt-4">
          <FormField
            label="Role (optional)"
            htmlFor="user-role"
            hint="Assign a role now, or skip this and use Manage Roles later."
          >
            <Select
              id="user-role"
              value={selectedRoleId}
              onValueChange={setSelectedRoleId}
              options={(roles ?? []).map((r) => ({ value: r.id, label: r.name }))}
              placeholder="No role"
            />
          </FormField>
        </div>
      </form>
    </EntityDrawer>
  );
}
