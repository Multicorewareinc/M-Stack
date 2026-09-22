import { EntityDrawer, RoleSelector } from '@multistack/ui';
import * as React from 'react';
import { ApiError } from '../../api/client';
import { useToast } from '../../app/ToastProvider';
import { useRolesForSelect } from './hooks/useRolesForSelect';
import { useSetUserRoles, useUserRoles } from './hooks/useUserRoles';

export interface ManageRolesDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  userId: string;
}

/** Role assignment is always a dedicated operation — never bundled into create/edit (§60). */
export function ManageRolesDrawer({ open, onOpenChange, userId }: ManageRolesDrawerProps) {
  const { data: roles } = useRolesForSelect();
  const { data: currentRoles, isLoading } = useUserRoles(userId);
  const setUserRoles = useSetUserRoles(userId);
  const toast = useToast();

  const [selectedIds, setSelectedIds] = React.useState<string[]>([]);
  // Tracks which role-id set selectedIds was last synced from, so a
  // subsequent refetch with the same data doesn't clobber in-progress edits.
  const syncedForRef = React.useRef<string | null>(null);

  // useLayoutEffect (not useEffect) so this sync commits within the same
  // render/act() batch that made currentRoles available — no intermediate
  // frame where the checkboxes are observably unchecked before "catching up".
  React.useLayoutEffect(() => {
    if (!currentRoles) return;
    const key = currentRoles.role_ids.join(',');
    if (syncedForRef.current !== key) {
      setSelectedIds(currentRoles.role_ids);
      syncedForRef.current = key;
    }
  }, [currentRoles]);

  React.useLayoutEffect(() => {
    if (!open) syncedForRef.current = null;
  }, [open]);

  async function handleSubmit() {
    try {
      await setUserRoles.mutateAsync(selectedIds);
      onOpenChange(false);
      toast.success('Roles updated');
    } catch (err) {
      toast.error('Unable to update roles', err instanceof ApiError ? err.message : undefined);
    }
  }

  const isDirty = currentRoles
    ? JSON.stringify([...selectedIds].sort()) !== JSON.stringify([...currentRoles.role_ids].sort())
    : false;

  return (
    <EntityDrawer
      open={open}
      onOpenChange={onOpenChange}
      title="Manage Roles"
      loading={isLoading}
      dirty={isDirty}
      submitting={setUserRoles.isPending}
      onSubmit={handleSubmit}
      submitLabel="Save Roles"
    >
      <RoleSelector
        roles={(roles ?? []).map((r) => ({ id: r.id, name: r.name, description: r.description ?? undefined }))}
        selectedIds={selectedIds}
        onChange={setSelectedIds}
      />
    </EntityDrawer>
  );
}
