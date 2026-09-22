import { zodResolver } from '@hookform/resolvers/zod';
import { Alert, Button, FormField, Input } from '@multistack/ui';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import type { ChangePasswordInput } from './api';

const schema = z
  .object({
    current_password: z.string().min(1, 'Current password is required.'),
    new_password: z.string().min(12, 'Password must be at least 12 characters.'),
    confirm_password: z.string().min(1, 'Please confirm your new password.'),
  })
  .refine((v) => v.new_password === v.confirm_password, {
    message: 'Passwords do not match.',
    path: ['confirm_password'],
  });
type FormValues = z.infer<typeof schema>;

export interface ChangePasswordPageProps {
  onSubmit: (input: ChangePasswordInput) => void;
  submitting: boolean;
  error: string | null;
}

/**
 * Forced gate: rendered by AuthProvider in place of the whole app when the signed-in session's
 * must_change_password is true (§ modules.auth PASSWORD_CHANGE_REQUIRED). No cancel/back —
 * changing the password is the only way out, mirroring the backend's own confinement of such a
 * principal.
 */
export function ChangePasswordPage({ onSubmit, submitting, error }: ChangePasswordPageProps) {
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<FormValues>({ resolver: zodResolver(schema) });

  const submit = handleSubmit(({ current_password, new_password }) =>
    onSubmit({ current_password, new_password }),
  );

  return (
    <div className="flex h-screen items-center justify-center bg-neutral-50">
      <form
        onSubmit={submit}
        className="flex w-full max-w-sm flex-col gap-4 rounded-lg border border-subtle bg-neutral-0 p-6 shadow-md"
      >
        <div className="flex flex-col gap-1">
          <h1 className="text-pageTitle font-semibold text-neutral-900">Change your password</h1>
          <p className="text-secondary text-neutral-500">You must set a new password before continuing.</p>
        </div>
        {error && (
          <Alert variant="error" title="Unable to change password">
            {error}
          </Alert>
        )}
        <FormField label="Current password" htmlFor="cp-current" error={errors.current_password?.message} required>
          <Input id="cp-current" type="password" autoComplete="current-password" {...register('current_password')} />
        </FormField>
        <FormField label="New password" htmlFor="cp-new" error={errors.new_password?.message} required>
          <Input id="cp-new" type="password" autoComplete="new-password" {...register('new_password')} />
        </FormField>
        <FormField
          label="Confirm new password"
          htmlFor="cp-confirm"
          error={errors.confirm_password?.message}
          required
        >
          <Input id="cp-confirm" type="password" autoComplete="new-password" {...register('confirm_password')} />
        </FormField>
        <Button type="submit" loading={submitting} disabled={submitting}>
          Update password
        </Button>
      </form>
    </div>
  );
}
