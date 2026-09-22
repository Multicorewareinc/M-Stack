import { Button, DataTable, PageHeader, SearchInput, Select, StatusBadge, type Column, type RowAction } from '@multistack/ui';
import * as React from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { Can } from '../../components/Can';
import type { User } from '../../api/types';
import { formatDate } from '../../lib/formatters';
import { CreateUserDrawer } from './CreateUserDrawer';
import { DeleteUserDialog } from './DeleteUserDialog';
import { useUsers } from './hooks/useUsers';

const STATUS_OPTIONS = [
  { value: '', label: 'All statuses' },
  { value: 'active', label: 'Active' },
  { value: 'suspended', label: 'Suspended' },
];

export function UsersPage() {
  const [search, setSearch] = React.useState('');
  const [status, setStatus] = React.useState('');
  const [createOpen, setCreateOpen] = React.useState(false);
  const [deleteTarget, setDeleteTarget] = React.useState<User | null>(null);
  const navigate = useNavigate();
  const location = useLocation();

  const { data: users, isLoading, error, refetch } = useUsers();

  const filtered = React.useMemo(() => {
    if (!users) return [];
    return users.filter((u) => {
      const matchesSearch = !search || (u.display_name ?? u.username).toLowerCase().includes(search.toLowerCase());
      const matchesStatus = !status || u.status === status;
      return matchesSearch && matchesStatus;
    });
  }, [users, search, status]);

  const columns: Column<User>[] = [
    { key: 'name', header: 'Name', accessor: (u) => u.display_name ?? u.username },
    { key: 'email', header: 'Email', accessor: (u) => u.email },
    { key: 'status', header: 'Status', render: (u) => <StatusBadge status={u.status} /> },
    { key: 'updated', header: 'Updated', accessor: (u) => formatDate(u.updated_at) },
  ];

  return (
    <div>
      <PageHeader
        title="Users"
        actions={
          <Can permission="users.create">
            <Button onClick={() => setCreateOpen(true)}>+ Add User</Button>
          </Can>
        }
      />
      <div className="mb-4 flex gap-2">
        <SearchInput
          aria-label="Search users"
          placeholder="Search users..."
          onValueChange={setSearch}
          className="max-w-sm"
        />
        <Select aria-label="Status" value={status} onValueChange={setStatus} options={STATUS_OPTIONS} className="w-40" />
      </div>
      <DataTable
        columns={columns}
        rows={filtered}
        getRowId={(u) => u.id}
        loading={isLoading}
        error={error ? error.message : null}
        onRetry={() => refetch()}
        onRowClick={(u) => navigate(`/users/${u.id}`, { state: { backgroundLocation: location } })}
        rowActions={(u) => {
          const actions: RowAction<User>[] = [
            { label: 'View', onClick: () => navigate(`/users/${u.id}`, { state: { backgroundLocation: location } }) },
          ];
          actions.push({ label: 'Delete', destructive: true, onClick: () => setDeleteTarget(u) });
          return actions;
        }}
        empty={{ title: 'No users yet' }}
      />
      <CreateUserDrawer open={createOpen} onOpenChange={setCreateOpen} />
      {deleteTarget && (
        <DeleteUserDialog
          open={Boolean(deleteTarget)}
          onOpenChange={(open) => !open && setDeleteTarget(null)}
          user={deleteTarget}
        />
      )}
    </div>
  );
}
