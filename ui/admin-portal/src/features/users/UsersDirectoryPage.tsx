import { PageHeader, SearchInput } from '@multistack/ui';
import * as React from 'react';
import { useOrganizations } from '../organizations/hooks/useOrganizations';
import { UserDirectoryTable } from './UserDirectoryTable';
import { useUserDirectory } from './hooks/useUserDirectory';

export function UsersDirectoryPage() {
  const [search, setSearch] = React.useState('');
  const { data: users, isLoading, error, refetch } = useUserDirectory();
  const { data: organizations } = useOrganizations();

  const filtered = React.useMemo(() => {
    if (!users) return [];
    if (!search) return users;
    const q = search.toLowerCase();
    return users.filter((u) => (u.display_name ?? u.username).toLowerCase().includes(q));
  }, [users, search]);

  return (
    <div>
      <PageHeader
        eyebrow="Platform Control Plane / Users"
        title="Users"
        description="Platform-wide user directory."
      />
      <SearchInput
        aria-label="Search users"
        placeholder="Search users..."
        onValueChange={setSearch}
        className="mb-4 max-w-sm"
      />
      <UserDirectoryTable
        rows={filtered}
        loading={isLoading}
        error={error ? error.message : null}
        onRetry={() => refetch()}
        organizations={organizations}
      />
    </div>
  );
}
