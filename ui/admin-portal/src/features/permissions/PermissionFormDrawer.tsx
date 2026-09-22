import { zodResolver } from '@hookform/resolvers/zod';
import { EntityDrawer, FormField, Input, Textarea } from '@multistack/ui';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { ApiError } from '../../api/client';
import type { Permission } from '../../api/types';
import { useToast } from '../../app/ToastProvider';
import { useCreatePermission } from './hooks/useCreatePermission';
import { useUpdatePermission } from './hooks/useUpdatePermission';

const schema = z.object({
  resource: z.string().min(1, 'Resource is required.'),
  action: z.string().min(1, 'Action is required.'),
  description: z.string().optional(),
});
type FormValues = z.infer<typeof schema>;

const EMPTY_VALUES: FormValues = { resource: '', action: '', description: '' };

export interface PermissionFormDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Present -> edit mode, pre-filled with this permission's current values. Absent -> create mode. */
  permission?: Permission;
}

export function PermissionFormDrawer({ open, onOpenChange, permission }: PermissionFormDrawerProps) {
  const isEdit = Boolean(permission);
  const createPermission = useCreatePermission();
  const updatePermission = useUpdatePermission(permission?.id ?? '');
  const toast = useToast();

  const {
    register,
    handleSubmit,
    setError,
    reset,
    watch,
    formState: { errors, isDirty },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    values: permission
      ? { resource: permission.resource, action: permission.action, description: permission.description ?? '' }
      : EMPTY_VALUES,
  });

  const mutation = isEdit ? updatePermission : createPermission;
  const [resource, action] = watch(['resource', 'action']);
  // Slug is always server-computed as `{resource}.{action}` (never a client input, on create or
  // update) — this is a live preview only, not a submitted field.
  const slugPreview = resource && action ? `${resource}.${action}` : permission?.slug;

  const onSubmit = handleSubmit(async (values) => {
    try {
      if (isEdit) {
        await updatePermission.mutateAsync(values);
      } else {
        await createPermission.mutateAsync(values);
      }
      reset(EMPTY_VALUES);
      onOpenChange(false);
      toast.success(isEdit ? 'Permission updated' : 'Permission created', slugPreview);
    } catch (err) {
      if (err instanceof ApiError && err.field) {
        setError(err.field as keyof FormValues, { message: err.message });
      } else {
        toast.error(
          isEdit ? 'Unable to update permission' : 'Unable to create permission',
          err instanceof ApiError ? err.message : undefined,
        );
      }
    }
  });

  return (
    <EntityDrawer
      open={open}
      onOpenChange={onOpenChange}
      title={isEdit ? 'Edit Permission' : 'Create Permission'}
      dirty={isDirty}
      submitting={mutation.isPending}
      onSubmit={onSubmit}
      submitLabel={isEdit ? 'Save Changes' : 'Create Permission'}
    >
      <form className="flex flex-col gap-4" onSubmit={onSubmit}>
        <FormField label="Resource" htmlFor="perm-resource" error={errors.resource?.message} required>
          <Input {...register('resource')} placeholder="users" />
        </FormField>
        <FormField label="Action" htmlFor="perm-action" error={errors.action?.message} required>
          <Input {...register('action')} placeholder="read" />
        </FormField>
        {slugPreview && (
          <p className="text-secondary text-neutral-500">
            Slug: <span className="font-mono">{slugPreview}</span>
          </p>
        )}
        <FormField label="Description" htmlFor="perm-description">
          <Textarea {...register('description')} placeholder="Optional" />
        </FormField>
      </form>
    </EntityDrawer>
  );
}
