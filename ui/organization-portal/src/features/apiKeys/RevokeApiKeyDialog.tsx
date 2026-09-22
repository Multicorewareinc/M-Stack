import { ConfirmDialog } from '@multistack/ui';
import { ApiError } from '../../api/client';
import type { ApiKey } from '../../api/apiKeys';
import { useToast } from '../../app/ToastProvider';
import { useRevokeApiKey } from './hooks/useRevokeApiKey';

export interface RevokeApiKeyDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  apiKey: ApiKey;
}

/** A revoked key stops verifying (model-gateway checks revoked_at on every request) but stays
 * listed — an audit trail. There's no separate hard-delete on the real backend; DELETE IS revoke. */
export function RevokeApiKeyDialog({ open, onOpenChange, apiKey }: RevokeApiKeyDialogProps) {
  const revokeApiKey = useRevokeApiKey();
  const toast = useToast();

  async function handleConfirm() {
    try {
      await revokeApiKey.mutateAsync(apiKey.id);
      onOpenChange(false);
      toast.success('API key revoked', apiKey.name);
    } catch (err) {
      toast.error('Unable to revoke API key', err instanceof ApiError ? err.message : undefined);
    }
  }

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Revoke "${apiKey.name}"?`}
      description="Anything using this key will stop working immediately. This cannot be undone."
      confirmLabel="Revoke"
      destructive
      loading={revokeApiKey.isPending}
      onConfirm={handleConfirm}
    />
  );
}
