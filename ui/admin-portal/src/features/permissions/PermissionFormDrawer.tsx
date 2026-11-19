import { zodResolver } from '@hookform/resolvers/zod';
import { EntityDrawer, FormField, Select, Textarea } from '@multistack/ui';
import { Controller, useForm } from 'react-hook-form';
import { z } from 'zod';
import { ApiError } from '../../api/client';
import type { Permission } from '../../api/types';
import { useToast } from '../../app/ToastProvider';
import { useCreatePermission } from './hooks/useCreatePermission';
import { useUpdatePermission } from './hooks/useUpdatePermission';

// Mirrors the master permission catalog seeded by admin-control-plane (seed.py
// DEFAULT_PERMISSIONS, rbac doc §26 / ADR-019). The API itself accepts any non-empty
// string for both fields, so this is a UI-side allowlist rather than a server contract.
const RESOURCE_OPTIONS = [
  { value: 'apikey', label: 'API Key' },
  { value: 'organizations', label: 'Organizations' },
  { value: 'roles', label: 'Roles' },
  { value: 'users', label: 'Users' },
];

const ACTION_LABELS: Record<string, string> = {
  create: 'Create',
  read: 'Read',
  update: 'Update',
  delete: 'Delete',
};

// Which actions are valid per resource (mirrors DEFAULT_PERMISSIONS in seed.py) — e.g.
// `organizations` has no create/delete.
const ACTIONS_BY_RESOURCE: Record<string, string[]> = {
  users: ['create', 'read', 'update', 'delete'],
  roles: ['create', 'read', 'update', 'delete'],
  organizations: ['read', 'update'],
  apikey: ['create', 'update', 'delete'],
};

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
    control,
    setError,
    setValue,
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
  const actionOptions = (ACTIONS_BY_RESOURCE[resource] ?? []).map((value) => ({
    value,
    label: ACTION_LABELS[value],
  }));
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
          {/* FormField clones its direct child to inject id/aria-* — but Controller doesn't
              forward unknown props into its render prop, so those must be passed explicitly here. */}
          <Controller
            control={control}
            name="resource"
            render={({ field }) => (
              <Select
                id="perm-resource"
                aria-invalid={errors.resource ? true : undefined}
                value={field.value}
                onValueChange={(value) => {
                  field.onChange(value);
                  // A resource change can invalidate the current action (e.g. switching to
                  // `organizations`, which has no `create`/`delete`) — clear it rather than leave
                  // a stale pair.
                  if (!ACTIONS_BY_RESOURCE[value]?.includes(action)) {
                    setValue('action', '', { shouldDirty: true, shouldValidate: false });
                  }
                }}
                options={RESOURCE_OPTIONS}
                placeholder="Select a resource"
              />
            )}
          />
        </FormField>
        <FormField label="Action" htmlFor="perm-action" error={errors.action?.message} required>
          <Controller
            control={control}
            name="action"
            render={({ field }) => (
              <Select
                id="perm-action"
                aria-invalid={errors.action ? true : undefined}
                value={field.value}
                onValueChange={field.onChange}
                options={actionOptions}
                disabled={!resource}
                placeholder={resource ? 'Select an action' : 'Select a resource first'}
              />
            )}
          />
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
