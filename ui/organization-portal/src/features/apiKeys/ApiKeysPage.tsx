import { Badge, Button, DataTable, EmptyState, PageHeader, type Column, type RowAction } from '@multistack/ui';
import * as React from 'react';
import type { ApiKey, ApiKeyCreated } from '../../api/apiKeys';
import { shouldUseRealAuth } from '../../auth/env';
import { isChatKey } from '../chat/chatKey';
import { formatDate } from '../../lib/formatters';
import { CreateApiKeyDrawer } from './CreateApiKeyDrawer';
import { EditApiKeyDrawer } from './EditApiKeyDrawer';
import { useApiKeys } from './hooks/useApiKeys';
import { RevealApiKeyDialog } from './RevealApiKeyDialog';
import { RevokeApiKeyDialog } from './RevokeApiKeyDialog';
import { RotateApiKeyDialog } from './RotateApiKeyDialog';

const STATUS_VARIANT: Record<ApiKey['status'], 'success' | 'warning' | 'neutral'> = {
  active: 'success',
  expired: 'warning',
  revoked: 'neutral',
};
const STATUS_LABEL: Record<ApiKey['status'], string> = {
  active: 'Active',
  expired: 'Expired',
  revoked: 'Revoked',
};

function ApiKeysTable() {
  const { data: allKeys, isLoading, error, refetch } = useApiKeys();
  const keys = React.useMemo(() => (allKeys ?? []).filter((k) => !isChatKey(k.name)), [allKeys]);
  const [createOpen, setCreateOpen] = React.useState(false);
  const [revealKey, setRevealKey] = React.useState<string | null>(null);
  const [editTarget, setEditTarget] = React.useState<ApiKey | null>(null);
  const [rotateTarget, setRotateTarget] = React.useState<ApiKey | null>(null);
  const [revokeTarget, setRevokeTarget] = React.useState<ApiKey | null>(null);

  function handleIssued(created: ApiKeyCreated) {
    setRevealKey(created.raw_key);
  }

  const columns: Column<ApiKey>[] = [
    { key: 'name', header: 'Name', accessor: (k) => k.name },
    { key: 'prefix', header: 'Key', render: (k) => <span className="font-mono">{k.prefix}••••••••</span> },
    {
      key: 'status',
      header: 'Status',
      render: (k) => <Badge variant={STATUS_VARIANT[k.status]}>{STATUS_LABEL[k.status]}</Badge>,
    },
    { key: 'created', header: 'Created', accessor: (k) => formatDate(k.created_at) },
    { key: 'expires', header: 'Expires', accessor: (k) => (k.expires_at ? formatDate(k.expires_at) : '—') },
  ];

  return (
    <div>
      <PageHeader
        title="API Keys"
        description="Create keys for programmatic access to this organization."
        actions={<Button onClick={() => setCreateOpen(true)}>+ Create API Key</Button>}
      />
      <DataTable
        columns={columns}
        rows={keys ?? []}
        getRowId={(k) => k.id}
        loading={isLoading}
        error={error ? error.message : null}
        onRetry={() => refetch()}
        rowActions={(k) => {
          if (k.status === 'revoked') return [];
          const actions: RowAction<ApiKey>[] = [
            { label: 'Edit', onClick: () => setEditTarget(k) },
            { label: 'Rotate', onClick: () => setRotateTarget(k) },
          ];
          actions.push({ label: 'Revoke', destructive: true, onClick: () => setRevokeTarget(k) });
          return actions;
        }}
        empty={{
          title: 'No API keys yet',
          description: 'Create a key to allow programmatic access to this organization.',
          action: { label: 'Create API Key', onClick: () => setCreateOpen(true) },
        }}
      />
      <CreateApiKeyDrawer open={createOpen} onOpenChange={setCreateOpen} onCreated={handleIssued} />
      <RevealApiKeyDialog rawKey={revealKey} onClose={() => setRevealKey(null)} />
      {editTarget && (
        <EditApiKeyDrawer
          open={Boolean(editTarget)}
          onOpenChange={(open) => !open && setEditTarget(null)}
          apiKey={editTarget}
        />
      )}
      {rotateTarget && (
        <RotateApiKeyDialog
          open={Boolean(rotateTarget)}
          onOpenChange={(open) => !open && setRotateTarget(null)}
          apiKey={rotateTarget}
          onRotated={(rotated) => {
            setRotateTarget(null);
            handleIssued(rotated);
          }}
        />
      )}
      {revokeTarget && (
        <RevokeApiKeyDialog
          open={Boolean(revokeTarget)}
          onOpenChange={(open) => !open && setRevokeTarget(null)}
          apiKey={revokeTarget}
        />
      )}
    </div>
  );
}

/**
 * Backed by modules/api_keys on organization-control-plane — an end-user JWT surface
 * (get_current_user), not the shared SERVICE_API_KEY/VITE_DEV_API_KEY bridge every other page in
 * this portal can fall back to. There is nothing for this page to call under MockAuthProvider, so
 * it's gated on shouldUseRealAuth() rather than silently 401ing.
 */
export function ApiKeysPage() {
  if (!shouldUseRealAuth()) {
    return (
      <div>
        <PageHeader title="API Keys" />
        <EmptyState
          title="Requires a real signed-in session"
          description="API keys are tied to your own identity, not the shared development key this environment is currently using. Sign in with VITE_REAL_AUTH enabled to manage keys."
        />
      </div>
    );
  }
  return <ApiKeysTable />;
}
