import { zodResolver } from '@hookform/resolvers/zod';
import { EntityDrawer, FormField, Input, Textarea } from '@multistack/ui';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { ApiError } from '../../api/client';
import type { Role } from '../../api/types';
import { useToast } from '../../app/ToastProvider';
import { useCreateRole } from './hooks/useCreateRole';
import { useUpdateRole } from './hooks/useUpdateRole';

const schema = z.object({
  name: z.string().min(1, 'Role name is required.'),
  description: z.string().optional(),
});
type FormValues = z.infer<typeof schema>;

const EMPTY_VALUES: FormValues = { name: '', description: '' };

export interface RoleFormDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Present -> edit mode, pre-filled with this role's current values. Absent -> create mode. */
  role?: Role;
}

// Editability follows the backend response, never a client-side "system
// roles are immutable" assumption (§111) — this form is always attempted;
// a rejected mutation surfaces like any other conflict.
export function RoleFormDrawer({ open, onOpenChange, role }: RoleFormDrawerProps) {
  const isEdit = Boolean(role);
  const createRole = useCreateRole();
  const updateRole = useUpdateRole(role?.id ?? '');
  const toast = useToast();

  const {
    register,
    handleSubmit,
    setError,
    reset,
    formState: { errors, isDirty },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    values: role ? { name: role.name, description: role.description ?? '' } : EMPTY_VALUES,
  });

  const mutation = isEdit ? updateRole : createRole;

  const onSubmit = handleSubmit(async (values) => {
    try {
      if (isEdit) {
        await updateRole.mutateAsync(values);
      } else {
        await createRole.mutateAsync(values);
      }
      reset(EMPTY_VALUES);
      onOpenChange(false);
      toast.success(isEdit ? 'Role updated' : 'Role created', values.name);
    } catch (err) {
      if (err instanceof ApiError && err.field) {
        setError(err.field as keyof FormValues, { message: err.message });
      } else {
        toast.error(isEdit ? 'Unable to update role' : 'Unable to create role', err instanceof ApiError ? err.message : undefined);
      }
    }
  });

  return (
    <EntityDrawer
      open={open}
      onOpenChange={onOpenChange}
      title={isEdit ? 'Edit Role' : 'Create Role'}
      dirty={isDirty}
      submitting={mutation.isPending}
      onSubmit={onSubmit}
      submitLabel={isEdit ? 'Save Changes' : 'Create Role'}
    >
      <form className="flex flex-col gap-4" onSubmit={onSubmit}>
        <FormField label="Name" htmlFor="role-name" error={errors.name?.message} required>
          <Input {...register('name')} />
        </FormField>
        <FormField label="Description" htmlFor="role-description">
          <Textarea {...register('description')} placeholder="Optional" />
        </FormField>
      </form>
    </EntityDrawer>
  );
}
