import { Badge, DataTable, PageHeader, SearchInput, type Column } from '@multistack/ui';
import * as React from 'react';
import type { Permission } from '../../api/types';
import { usePermissionsCatalog } from './hooks/usePermissionsCatalog';

const columns: Column<Permission>[] = [
  { key: 'resource', header: 'Resource', accessor: (p) => p.resource },
  { key: 'action', header: 'Action', accessor: (p) => p.action },
  { key: 'slug', header: 'Slug', accessor: (p) => p.slug },
  { key: 'description', header: 'Description', accessor: (p) => p.description ?? '—' },
  {
    key: 'status',
    header: 'Status',
    render: (p) => <Badge variant={p.is_active ? 'success' : 'neutral'}>{p.is_active ? 'Active' : 'Inactive'}</Badge>,
  },
];

/** Platform-owned catalog, browsed read-only from the Organization Portal (§70-71). */
export function PermissionsPage() {
  const [search, setSearch] = React.useState('');
  const { data: permissions, isLoading, error, refetch } = usePermissionsCatalog();

  const filtered = React.useMemo(() => {
    if (!permissions) return [];
    if (!search) return permissions;
    const q = search.toLowerCase();
    return permissions.filter(
      (p) =>
        p.resource.toLowerCase().includes(q) ||
        p.action.toLowerCase().includes(q) ||
        p.slug.toLowerCase().includes(q) ||
        (p.description ?? '').toLowerCase().includes(q),
    );
  }, [permissions, search]);

  return (
    <div>
      <PageHeader title="Permissions" actions={<Badge variant="neutral">Read-only</Badge>} />
      <div className="mb-4 flex gap-2">
        <SearchInput
          aria-label="Search permissions"
          placeholder="Search permissions..."
          onValueChange={setSearch}
          className="max-w-sm"
        />
      </div>
      <DataTable
        columns={columns}
        rows={filtered}
        getRowId={(p) => p.id}
        loading={isLoading}
        error={error ? error.message : null}
        onRetry={() => refetch()}
        empty={{ title: 'No permissions found', description: 'Try a different search.' }}
      />
    </div>
  );
}
