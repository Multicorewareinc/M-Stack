import { zodResolver } from '@hookform/resolvers/zod';
import { EntityDrawer, FormField, Input, PermissionMatrix, SearchInput, Textarea } from '@multistack/ui';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import * as React from 'react';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { listPermissions } from '../../api/permissions';
import { ApiError } from '../../api/client';
import { queryKeys } from '../../api/queryKeys';
import { setRolePermissions } from '../../api/roles';
import type { Role } from '../../api/types';
import { useToast } from '../../app/ToastProvider';
import { useOrgId } from '../users/hooks/useOrgId';
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
//
// Permission selection is offered only in CREATE mode (§62 covers create as name+description
// only, but a brand-new role otherwise has zero permissions until someone visits its detail
// page's Permissions tab — letting the creator pick permissions right here removes that empty
// gap). Edit mode still points at the dedicated Role Detail > Permissions tab (§65-69), which
// stays the single place permissions are ever composed/changed after creation.
export function RoleFormDrawer({ open, onOpenChange, role }: RoleFormDrawerProps) {
  const isEdit = Boolean(role);
  const createRole = useCreateRole();
  const updateRole = useUpdateRole(role?.id ?? '');
  const toast = useToast();
  const queryClient = useQueryClient();
  const orgId = useOrgId();

  const { data: catalog } = useQuery({
    queryKey: ['permissions-catalog'],
    queryFn: () => listPermissions(),
    enabled: !isEdit && open,
  });
  const [search, setSearch] = React.useState('');
  const [selectedIds, setSelectedIds] = React.useState<string[]>([]);
  const [savingPermissions, setSavingPermissions] = React.useState(false);

  // A closed-without-saving create attempt shouldn't leave its permission picks armed for next
  // time the drawer opens — this state lives outside react-hook-form's own reset(), which only
  // covers name/description.
  React.useEffect(() => {
    if (open && !isEdit) {
      setSelectedIds([]);
      setSearch('');
    }
  }, [open, isEdit]);

  const filteredCatalog = React.useMemo(() => {
    if (!catalog) return [];
    if (!search) return catalog;
    const q = search.toLowerCase();
    return catalog.filter(
      (p) =>
        p.resource.toLowerCase().includes(q) ||
        p.action.toLowerCase().includes(q) ||
        p.slug.toLowerCase().includes(q) ||
        (p.description ?? '').toLowerCase().includes(q),
    );
  }, [catalog, search]);

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

  function resetAll() {
    reset(EMPTY_VALUES);
    setSelectedIds([]);
    setSearch('');
  }

  const onSubmit = handleSubmit(async (values) => {
    try {
      if (isEdit) {
        await updateRole.mutateAsync(values);
        onOpenChange(false);
        toast.success('Role updated', values.name);
        return;
      }

      const created = await createRole.mutateAsync(values);
      if (selectedIds.length > 0) {
        setSavingPermissions(true);
        try {
          await setRolePermissions(created.id, selectedIds);
          queryClient.invalidateQueries({ queryKey: queryKeys.rolePermissions(orgId, created.id) });
          queryClient.invalidateQueries({ queryKey: queryKeys.roles(orgId) });
        } catch (err) {
          // The role itself was created successfully — only the permission attach failed. Don't
          // present this as "role creation failed": say what actually happened, and leave the
          // role for the user to fix up from its own Permissions tab (same recovery path as any
          // other permission-save failure, §69 "No changes were applied" semantics — here it's
          // "no permissions were applied to the new role", not "no role was created").
          toast.error(
            'Role created, but permissions could not be saved',
            err instanceof ApiError ? `${err.message} — add them from the role's Permissions tab.` : undefined,
          );
          resetAll();
          onOpenChange(false);
          return;
        } finally {
          setSavingPermissions(false);
        }
      }
      resetAll();
      onOpenChange(false);
      toast.success('Role created', values.name);
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
      dirty={isDirty || selectedIds.length > 0}
      submitting={mutation.isPending || savingPermissions}
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

        {!isEdit && (
          <div className="flex flex-col gap-3 border-t border-subtle pt-4">
            <div className="flex flex-col gap-1">
              <span className="text-secondary font-medium text-neutral-700">Permissions (optional)</span>
              <p className="text-caption text-neutral-500">
                Select permissions from the platform master catalog, or skip this and add them later from
                the role's Permissions tab.
              </p>
            </div>
            <SearchInput
              aria-label="Search permissions"
              placeholder="Search permissions..."
              onValueChange={setSearch}
              className="max-w-sm"
            />
            <PermissionMatrix permissions={filteredCatalog} selectedIds={selectedIds} onChange={setSelectedIds} />
            {selectedIds.length > 0 && (
              <span className="text-secondary text-neutral-500">{selectedIds.length} permissions selected</span>
            )}
          </div>
        )}
      </form>
    </EntityDrawer>
  );
}
