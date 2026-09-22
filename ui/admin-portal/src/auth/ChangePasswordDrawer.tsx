import { zodResolver } from '@hookform/resolvers/zod';
import { EntityDrawer, FormField, Input } from '@multistack/ui';
import * as React from 'react';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { ApiError } from '../api/client';
import { useToast } from '../app/ToastProvider';
import { changePassword } from './api';
import { useAuth } from './AuthContext';

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

export interface ChangePasswordDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * Voluntary change-password, opened from the account menu — distinct from AuthProvider's forced
 * ChangePasswordPage (that one gates the whole app when must_change_password is true; this one is
 * available any time). Success has the same server-side effect either way — every refresh token
 * for this account is revoked (D5) — so this also ends in a sign-out, not a quiet dialog close.
 */
export function ChangePasswordDrawer({ open, onOpenChange }: ChangePasswordDrawerProps) {
  const { logout } = useAuth();
  const toast = useToast();
  const [submitting, setSubmitting] = React.useState(false);

  const {
    register,
    handleSubmit,
    reset,
    setError,
    formState: { errors, isDirty },
  } = useForm<FormValues>({ resolver: zodResolver(schema) });

  const onSubmit = handleSubmit(async ({ current_password, new_password }) => {
    setSubmitting(true);
    try {
      await changePassword({ current_password, new_password });
      reset();
      onOpenChange(false);
      toast.success('Password updated', 'Sign in again with your new password.');
      logout();
    } catch (err) {
      if (err instanceof ApiError && err.field) {
        setError(err.field as keyof FormValues, { message: err.message });
      } else {
        toast.error('Unable to change password', err instanceof ApiError ? err.message : undefined);
      }
    } finally {
      setSubmitting(false);
    }
  });

  return (
    <EntityDrawer
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
      title="Change Password"
      dirty={isDirty}
      submitting={submitting}
      onSubmit={onSubmit}
      submitLabel="Update Password"
    >
      <form className="flex flex-col gap-4" onSubmit={onSubmit}>
        <FormField label="Current password" htmlFor="cpd-current" error={errors.current_password?.message} required>
          <Input id="cpd-current" type="password" autoComplete="current-password" {...register('current_password')} />
        </FormField>
        <FormField label="New password" htmlFor="cpd-new" error={errors.new_password?.message} required>
          <Input id="cpd-new" type="password" autoComplete="new-password" {...register('new_password')} />
        </FormField>
        <FormField
          label="Confirm new password"
          htmlFor="cpd-confirm"
          error={errors.confirm_password?.message}
          required
        >
          <Input id="cpd-confirm" type="password" autoComplete="new-password" {...register('confirm_password')} />
        </FormField>
      </form>
    </EntityDrawer>
  );
}
