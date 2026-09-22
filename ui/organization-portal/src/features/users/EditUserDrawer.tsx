import { zodResolver } from '@hookform/resolvers/zod';
import { EntityDrawer, FormField, Input, Select } from '@multistack/ui';
import { Controller, useForm } from 'react-hook-form';
import { z } from 'zod';
import { ApiError } from '../../api/client';
import type { User } from '../../api/types';
import { useToast } from '../../app/ToastProvider';
import { useUpdateUser } from './hooks/useUpdateUser';

const schema = z.object({
  email: z.string().min(1, 'Email is required.').email('Enter a valid email address.'),
  first_name: z.string().optional(),
  last_name: z.string().optional(),
  display_name: z.string().optional(),
  status: z.string(),
});
type FormValues = z.infer<typeof schema>;

const STATUS_OPTIONS = [
  { value: 'active', label: 'Active' },
  { value: 'suspended', label: 'Suspended' },
];

export interface EditUserDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  user: User;
}

// Only backend-supported profile fields are editable — never the immutable
// username/id (§59).
export function EditUserDrawer({ open, onOpenChange, user }: EditUserDrawerProps) {
  const updateUser = useUpdateUser(user.id);
  const toast = useToast();

  const {
    register,
    handleSubmit,
    control,
    formState: { errors, isDirty },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    values: {
      email: user.email,
      first_name: user.first_name ?? '',
      last_name: user.last_name ?? '',
      display_name: user.display_name ?? '',
      status: user.status,
    },
  });

  const onSubmit = handleSubmit(async (values) => {
    try {
      await updateUser.mutateAsync(values);
      onOpenChange(false);
      toast.success('User updated', user.username);
    } catch (err) {
      toast.error('Unable to update user', err instanceof ApiError ? err.message : undefined);
    }
  });

  return (
    <EntityDrawer
      open={open}
      onOpenChange={onOpenChange}
      title="Edit User"
      dirty={isDirty}
      submitting={updateUser.isPending}
      onSubmit={onSubmit}
      submitLabel="Save Changes"
    >
      <form className="flex flex-col gap-4" onSubmit={onSubmit}>
        <div>
          <span className="text-secondary font-medium text-neutral-700">Username</span>
          <p className="font-mono text-body text-neutral-500">{user.username}</p>
        </div>
        <FormField label="Email" htmlFor="edit-user-email" error={errors.email?.message} required>
          <Input {...register('email')} />
        </FormField>
        <FormField label="First Name" htmlFor="edit-user-first-name">
          <Input {...register('first_name')} />
        </FormField>
        <FormField label="Last Name" htmlFor="edit-user-last-name">
          <Input {...register('last_name')} />
        </FormField>
        <FormField label="Display Name" htmlFor="edit-user-display-name">
          <Input {...register('display_name')} />
        </FormField>
        <FormField label="Status" htmlFor="edit-user-status">
          <Controller
            control={control}
            name="status"
            render={({ field }) => (
              <Select id="edit-user-status" value={field.value} onValueChange={field.onChange} options={STATUS_OPTIONS} />
            )}
          />
        </FormField>
      </form>
    </EntityDrawer>
  );
}
