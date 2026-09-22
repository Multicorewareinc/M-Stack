import { zodResolver } from '@hookform/resolvers/zod';
import { EntityDrawer, FormField, Input } from '@multistack/ui';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { ApiError } from '../../api/client';
import { useToast } from '../../app/ToastProvider';
import { usePermission } from '../../hooks/usePermission';
import { useCreateUser } from './hooks/useCreateUser';

const schema = z.object({
  username: z.string().min(1, 'Username is required.'),
  email: z.string().min(1, 'Email is required.').email('Enter a valid email address.'),
  first_name: z.string().optional(),
  last_name: z.string().optional(),
  display_name: z.string().optional(),
});
type FormValues = z.infer<typeof schema>;

const EMPTY_VALUES: FormValues = {
  username: '',
  email: '',
  first_name: '',
  last_name: '',
  display_name: '',
};

export interface CreateUserDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

// Deliberately no role field anywhere in this form — role assignment is
// always a separate operation via ManageRolesDrawer (§57, §60). No status
// field either — new users always start `active` server-side (UserCreate has
// no status field at all; only UserUpdate does).
export function CreateUserDrawer({ open, onOpenChange }: CreateUserDrawerProps) {
  const createUser = useCreateUser();
  const canCreate = usePermission('users.create');
  const toast = useToast();

  const {
    register,
    handleSubmit,
    setError,
    reset,
    formState: { errors, isDirty },
  } = useForm<FormValues>({ resolver: zodResolver(schema), defaultValues: EMPTY_VALUES });

  const onSubmit = handleSubmit(async (values) => {
    try {
      await createUser.mutateAsync(values);
      reset(EMPTY_VALUES);
      onOpenChange(false);
      toast.success('User created', values.username);
    } catch (err) {
      if (err instanceof ApiError && err.field) {
        setError(err.field as keyof FormValues, { message: err.message });
      } else {
        toast.error('Unable to create user', err instanceof ApiError ? err.message : undefined);
      }
    }
  });

  if (!canCreate) return null;

  return (
    <EntityDrawer
      open={open}
      onOpenChange={onOpenChange}
      title="Create User"
      dirty={isDirty}
      submitting={createUser.isPending}
      onSubmit={onSubmit}
      submitLabel="Create User"
    >
      <form className="flex flex-col gap-4" onSubmit={onSubmit}>
        <FormField label="Username" htmlFor="user-username" error={errors.username?.message} required>
          <Input {...register('username')} />
        </FormField>
        <FormField label="Email" htmlFor="user-email" error={errors.email?.message} required>
          <Input {...register('email')} />
        </FormField>
        <FormField label="First Name" htmlFor="user-first-name">
          <Input {...register('first_name')} />
        </FormField>
        <FormField label="Last Name" htmlFor="user-last-name">
          <Input {...register('last_name')} />
        </FormField>
        <FormField label="Display Name" htmlFor="user-display-name">
          <Input {...register('display_name')} />
        </FormField>
      </form>
    </EntityDrawer>
  );
}
