import { zodResolver } from '@hookform/resolvers/zod';
import { EntityDrawer, FormField, Input } from '@multistack/ui';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import type { ApiKey } from '../../api/apiKeys';
import { ApiError } from '../../api/client';
import { useToast } from '../../app/ToastProvider';
import { useUpdateApiKey } from './hooks/useUpdateApiKey';

const schema = z.object({
  name: z.string().min(1, 'Name is required.'),
  // Empty string means "no expiry" — HTML date inputs give '' when cleared, never null.
  expires_at: z.string().optional(),
});
type FormValues = z.infer<typeof schema>;

function toDateInputValue(iso: string | null): string {
  return iso ? iso.slice(0, 10) : '';
}

export interface EditApiKeyDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  apiKey: ApiKey;
}

/** Renames a key and/or changes its expiry — the secret itself is untouched (use Rotate for that).
 * The backend rejects editing an already-revoked key (409), so this is only ever opened for an
 * active/expired one (see ApiKeysPage's row-action gating). */
export function EditApiKeyDrawer({ open, onOpenChange, apiKey }: EditApiKeyDrawerProps) {
  const updateApiKey = useUpdateApiKey(apiKey.id);
  const toast = useToast();

  const {
    register,
    handleSubmit,
    reset,
    setError,
    formState: { errors, isDirty },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    values: { name: apiKey.name, expires_at: toDateInputValue(apiKey.expires_at) },
  });

  const onSubmit = handleSubmit(async ({ name, expires_at }) => {
    try {
      await updateApiKey.mutateAsync({
        name,
        expires_at: expires_at ? new Date(expires_at).toISOString() : null,
      });
      onOpenChange(false);
      toast.success('API key updated', name);
    } catch (err) {
      if (err instanceof ApiError && err.field) {
        setError(err.field as keyof FormValues, { message: err.message });
      } else {
        toast.error('Unable to update API key', err instanceof ApiError ? err.message : undefined);
      }
    }
  });

  return (
    <EntityDrawer
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
      title="Edit API Key"
      dirty={isDirty}
      submitting={updateApiKey.isPending}
      onSubmit={onSubmit}
      submitLabel="Save Changes"
    >
      <form className="flex flex-col gap-4" onSubmit={onSubmit}>
        <FormField label="Name" htmlFor="api-key-edit-name" error={errors.name?.message} required>
          <Input id="api-key-edit-name" {...register('name')} />
        </FormField>
        <FormField
          label="Expires (optional)"
          htmlFor="api-key-edit-expires"
          error={errors.expires_at?.message}
        >
          <Input id="api-key-edit-expires" type="date" {...register('expires_at')} />
        </FormField>
      </form>
    </EntityDrawer>
  );
}
