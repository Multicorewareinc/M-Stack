import { useQuery } from '@tanstack/react-query';
import { Button, PermissionMatrix, SearchInput } from '@multistack/ui';
import * as React from 'react';
import { ApiError } from '../../api/client';
import { listPermissions } from '../../api/permissions';
import { useToast } from '../../app/ToastProvider';
import { useRolePermissions, useSetRolePermissions } from './hooks/useRolePermissions';

export interface RolePermissionsTabProps {
  roleId: string;
}

/**
 * The composition screen (§65-69) — the most RBAC-critical UI in the
 * Organization Portal. Roles compose permissions from the platform master
 * catalog; they never author their own (§137-138).
 */
export function RolePermissionsTab({ roleId }: RolePermissionsTabProps) {
  const { data: catalog } = useQuery({ queryKey: ['permissions-catalog'], queryFn: () => listPermissions() });
  const { data: current } = useRolePermissions(roleId);
  const setRolePermissions = useSetRolePermissions(roleId);
  const toast = useToast();

  const [search, setSearch] = React.useState('');
  const [selectedIds, setSelectedIds] = React.useState<string[]>([]);
  // Sync commits within the same act() batch that made `current` available —
  // no intermediate frame where the matrix is observably out of sync with
  // the backend's current selection (see ManageRolesDrawer for the same pattern).
  const syncedForRef = React.useRef<string | null>(null);
  React.useLayoutEffect(() => {
    if (!current) return;
    const key = [...current.permission_ids].sort().join(',');
    if (syncedForRef.current !== key) {
      setSelectedIds(current.permission_ids);
      syncedForRef.current = key;
    }
  }, [current]);

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

  const initialIds = React.useMemo(() => new Set(current?.permission_ids ?? []), [current]);
  const selectedSet = React.useMemo(() => new Set(selectedIds), [selectedIds]);
  const added = selectedIds.filter((id) => !initialIds.has(id));
  const removed = [...initialIds].filter((id) => !selectedSet.has(id));

  function selectAllInResource(resource: string) {
    const idsInResource = (catalog ?? []).filter((p) => p.resource === resource).map((p) => p.id);
    setSelectedIds((prev) => [...new Set([...prev, ...idsInResource])]);
  }
  function clearResource(resource: string) {
    const idsInResource = new Set((catalog ?? []).filter((p) => p.resource === resource).map((p) => p.id));
    setSelectedIds((prev) => prev.filter((id) => !idsInResource.has(id)));
  }

  const resources = [...new Set((catalog ?? []).map((p) => p.resource))];

  async function handleSave() {
    // Bulk-select controls only ever affect selectedIds — this is the exact,
    // authoritative payload submitted (§68).
    try {
      await setRolePermissions.mutateAsync(selectedIds);
      toast.success('Permissions saved');
    } catch (err) {
      toast.error('Unable to save permissions', err instanceof ApiError ? err.message : undefined);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <p className="text-secondary text-neutral-500">
        Select permissions from the platform master catalog. These permissions are maintained by the Super Admin.
      </p>
      <SearchInput
        aria-label="Search permissions"
        placeholder="Search permissions..."
        onValueChange={setSearch}
        className="max-w-sm"
      />
      <div className="flex flex-wrap gap-2">
        {resources.map((resource) => (
          <div key={resource} className="flex items-center gap-1">
            <span className="text-caption capitalize text-neutral-500">{resource}:</span>
            <Button variant="ghost" size="sm" onClick={() => selectAllInResource(resource)}>
              Select all
            </Button>
            <Button variant="ghost" size="sm" onClick={() => clearResource(resource)}>
              Clear
            </Button>
          </div>
        ))}
      </div>
      <PermissionMatrix permissions={filteredCatalog} selectedIds={selectedIds} onChange={setSelectedIds} />
      {(added.length > 0 || removed.length > 0) && (
        <div className="rounded-md border border-subtle bg-neutral-50 p-3 text-secondary" data-testid="change-summary">
          <p className="font-medium text-neutral-700">Changes</p>
          {added.map((id) => (
            <p key={id} className="text-success-700">
              + {catalog?.find((p) => p.id === id)?.slug ?? id}
            </p>
          ))}
          {removed.map((id) => (
            <p key={id} className="text-danger-700">
              − {catalog?.find((p) => p.id === id)?.slug ?? id}
            </p>
          ))}
        </div>
      )}
      <div className="flex items-center justify-between border-t border-subtle pt-4">
        <span className="text-secondary text-neutral-500">{selectedIds.length} permissions selected</span>
        <Button onClick={handleSave} loading={setRolePermissions.isPending}>
          Save Permissions
        </Button>
      </div>
    </div>
  );
}
