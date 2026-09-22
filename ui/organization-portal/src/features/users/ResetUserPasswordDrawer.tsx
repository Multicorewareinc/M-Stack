import { zodResolver } from '@hookform/resolvers/zod';
import { Alert, EntityDrawer, FormField, Input } from '@multistack/ui';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { ApiError } from '../../api/client';
import type { User } from '../../api/types';
import { useToast } from '../../app/ToastProvider';
import { useUpdateUser } from './hooks/useUpdateUser';

const schema = z
  .object({
    new_password: z.string().min(12, 'Password must be at least 12 characters.'),
    confirm_password: z.string().min(1, 'Please confirm the new password.'),
  })
  .refine((v) => v.new_password === v.confirm_password, {
    message: 'Passwords do not match.',
    path: ['confirm_password'],
  });
type FormValues = z.infer<typeof schema>;

export interface ResetUserPasswordDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  user: User;
}

/**
 * Admin-initiated reset (no current password required — that's only for a user changing their
 * OWN password via /api/auth/password). Sets a new password and forces rotation on the user's
 * next login (must_change_password=True), the same semantic as CreateUserDrawer's optional
 * initial password and seed_admin's own bootstrap.
 */
export function ResetUserPasswordDrawer({ open, onOpenChange, user }: ResetUserPasswordDrawerProps) {
  const updateUser = useUpdateUser(user.id);
  const toast = useToast();

  const {
    register,
    handleSubmit,
    reset,
    setError,
    formState: { errors, isDirty },
  } = useForm<FormValues>({ resolver: zodResolver(schema) });

  const onSubmit = handleSubmit(async ({ new_password }) => {
    try {
      await updateUser.mutateAsync({ password: new_password });
      reset();
      onOpenChange(false);
      toast.success('Password reset', `${user.username} must set a new password at next sign-in.`);
    } catch (err) {
      if (err instanceof ApiError && err.field) {
        setError(err.field as keyof FormValues, { message: err.message });
      } else {
        toast.error('Unable to reset password', err instanceof ApiError ? err.message : undefined);
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
      title="Reset Password"
      dirty={isDirty}
      submitting={updateUser.isPending}
      onSubmit={onSubmit}
      submitLabel="Reset Password"
    >
      <form className="flex flex-col gap-4" onSubmit={onSubmit}>
        <Alert variant="warning">
          {user.username} will be required to set a new password at their next sign-in.
        </Alert>
        <FormField label="New password" htmlFor="reset-pw-new" error={errors.new_password?.message} required>
          <Input id="reset-pw-new" type="password" autoComplete="new-password" {...register('new_password')} />
        </FormField>
        <FormField
          label="Confirm new password"
          htmlFor="reset-pw-confirm"
          error={errors.confirm_password?.message}
          required
        >
          <Input
            id="reset-pw-confirm"
            type="password"
            autoComplete="new-password"
            {...register('confirm_password')}
          />
        </FormField>
      </form>
    </EntityDrawer>
  );
}
