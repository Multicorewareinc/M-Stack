import { DataTable, StatusBadge, type Column } from '@multistack/ui';
import * as React from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import type { UserDirectoryEntry } from '../../api/types';
import { formatDate } from '../../lib/formatters';

export interface UserDirectoryTableProps {
  rows: UserDirectoryEntry[];
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
  /** Resolves organization_id -> name for the Organization column; falls back to the raw id for
   * any org not present here (e.g. still loading, or deleted). */
  organizations?: { id: string; name: string }[];
}

/**
 * Shared read-only directory rendering (§42) — used by both the platform-wide
 * Users page and the Organization Detail page's Users tab, so the two
 * contexts never diverge in columns or empty/loading/error states. No row
 * actions: the directory is a projection: Admin CP owns no user mutation
 * (§209 CRUD matrix).
 */
export function UserDirectoryTable({ rows, loading, error, onRetry, organizations }: UserDirectoryTableProps) {
  const navigate = useNavigate();
  const location = useLocation();

  const orgNameById = React.useMemo(() => {
    const map = new Map<string, string>();
    for (const org of organizations ?? []) map.set(org.id, org.name);
    return map;
  }, [organizations]);

  const columns: Column<UserDirectoryEntry>[] = [
    { key: 'name', header: 'User', accessor: (u) => u.display_name ?? u.username },
    { key: 'email', header: 'Email', accessor: (u) => u.email },
    {
      key: 'organization',
      header: 'Organization',
      accessor: (u) => orgNameById.get(u.organization_id) ?? u.organization_id,
    },
    { key: 'status', header: 'Status', render: (u) => <StatusBadge status={u.status} /> },
    { key: 'updated', header: 'Updated', accessor: (u) => formatDate(u.updated_at) },
  ];

  return (
    <DataTable
      columns={columns}
      rows={rows}
      getRowId={(u) => u.id}
      loading={loading}
      error={error ?? null}
      onRetry={onRetry}
      onRowClick={(u) => navigate(`/users/${u.id}`, { state: { backgroundLocation: location } })}
      empty={{ title: 'No users yet' }}
    />
  );
}
