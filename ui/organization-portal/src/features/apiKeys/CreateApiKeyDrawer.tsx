import { zodResolver } from '@hookform/resolvers/zod';
import { EntityDrawer, FormField, Input } from '@multistack/ui';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { ApiError } from '../../api/client';
import { useToast } from '../../app/ToastProvider';
import { useCreateApiKey } from './hooks/useCreateApiKey';
import type { ApiKeyCreated } from '../../api/apiKeys';

const schema = z.object({
  name: z.string().min(1, 'Name is required.'),
  // Empty string means "no expiry" — HTML date inputs give '' when cleared, never null.
  expires_at: z.string().optional(),
});
type FormValues = z.infer<typeof schema>;

export interface CreateApiKeyDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Fires with the create response, which carries the raw secret exactly once. */
  onCreated: (created: ApiKeyCreated) => void;
}

export function CreateApiKeyDrawer({ open, onOpenChange, onCreated }: CreateApiKeyDrawerProps) {
  const createApiKey = useCreateApiKey();
  const toast = useToast();

  const {
    register,
    handleSubmit,
    reset,
    setError,
    formState: { errors, isDirty },
  } = useForm<FormValues>({ resolver: zodResolver(schema), defaultValues: { name: '', expires_at: '' } });

  const onSubmit = handleSubmit(async ({ name, expires_at }) => {
    try {
      const created = await createApiKey.mutateAsync({
        name,
        expires_at: expires_at ? new Date(expires_at).toISOString() : null,
      });
      reset();
      onOpenChange(false);
      onCreated(created);
    } catch (err) {
      if (err instanceof ApiError && err.field) {
        setError(err.field as keyof FormValues, { message: err.message });
      } else {
        toast.error('Unable to create API key', err instanceof ApiError ? err.message : undefined);
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
      title="Create API Key"
      dirty={isDirty}
      submitting={createApiKey.isPending}
      onSubmit={onSubmit}
      submitLabel="Create Key"
    >
      <form className="flex flex-col gap-4" onSubmit={onSubmit}>
        <FormField label="Name" htmlFor="api-key-name" error={errors.name?.message} required>
          <Input id="api-key-name" placeholder="e.g. CI pipeline" {...register('name')} />
        </FormField>
        <FormField label="Expires (optional)" htmlFor="api-key-expires" error={errors.expires_at?.message}>
          <Input id="api-key-expires" type="date" {...register('expires_at')} />
        </FormField>
      </form>
    </EntityDrawer>
  );
}
