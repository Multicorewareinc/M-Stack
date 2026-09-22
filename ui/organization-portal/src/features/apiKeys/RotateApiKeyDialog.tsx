import { ConfirmDialog } from '@multistack/ui';
import { ApiError } from '../../api/client';
import type { ApiKey, ApiKeyCreated } from '../../api/apiKeys';
import { useToast } from '../../app/ToastProvider';
import { useRotateApiKey } from './hooks/useRotateApiKey';

export interface RotateApiKeyDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  apiKey: ApiKey;
  /** Fires with the rotated response, which carries the new raw secret exactly once. */
  onRotated: (rotated: ApiKeyCreated) => void;
}

/** Regenerates the secret in place — same record (id, name, expiry), new value. The old secret
 * stops verifying immediately (the old hash is evicted from the gateway's cache post-commit). */
export function RotateApiKeyDialog({ open, onOpenChange, apiKey, onRotated }: RotateApiKeyDialogProps) {
  const rotateApiKey = useRotateApiKey();
  const toast = useToast();

  async function handleConfirm() {
    try {
      const rotated = await rotateApiKey.mutateAsync(apiKey.id);
      onOpenChange(false);
      onRotated(rotated);
    } catch (err) {
      toast.error('Unable to rotate API key', err instanceof ApiError ? err.message : undefined);
    }
  }

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Rotate "${apiKey.name}"?`}
      description="The current key stops working immediately and is replaced with a new secret, shown once."
      confirmLabel="Rotate"
      destructive
      loading={rotateApiKey.isPending}
      onConfirm={handleConfirm}
    />
  );
}
