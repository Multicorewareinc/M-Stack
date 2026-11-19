import { zodResolver } from '@hookform/resolvers/zod';
import { Alert, Button, Dialog, FormField, Input, Select } from '@multistack/ui';
import { Controller, useForm } from 'react-hook-form';
import { z } from 'zod';
import { ApiError } from '../../api/client';
import { inviteLink, type Invitation } from '../../api/invitations';
import { useRolesForSelect } from '../users/hooks/useRolesForSelect';
import { useCreateInvitation } from './hooks/useInvitations';

const schema = z.object({
  name: z.string().trim().min(1, 'Name is required.'),
  email: z
    .string()
    .trim()
    .min(1, 'Email is required.')
    .email('Enter a valid email address.')
    // The server enforces the same limits (RFC 5321): 254 characters overall, 64 before the "@".
    .refine((v) => v.length <= 254 && v.split('@')[0].length <= 64, 'Enter a valid email address.'),
  // Empty = let the server apply the organization's default member role.
  role_id: z.string(),
});
type FormValues = z.infer<typeof schema>;

const EMPTY: FormValues = { name: '', email: '', role_id: '' };

export interface InviteMemberDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (invitation: Invitation, link: string) => void;
}

/**
 * An org admin invites someone into THEIR OWN organization (the server takes the organization from the
 * signed-in session, never from the form). On success the dialog closes and the page shows the one-time
 * link to copy: the invitee opens it, chooses a password, and signs in. No email is sent.
 */
export function InviteMemberDialog({ open, onOpenChange, onCreated }: InviteMemberDialogProps) {
  const createInvitation = useCreateInvitation();
  const { data: roles } = useRolesForSelect();
  const {
    register,
    handleSubmit,
    control,
    setError,
    reset,
    formState: { errors },
  } = useForm<FormValues>({ resolver: zodResolver(schema), defaultValues: EMPTY });

  const onSubmit = handleSubmit(async (v) => {
    try {
      const invitation = await createInvitation.mutateAsync({
        email: v.email,
        name: v.name,
        ...(v.role_id ? { role_id: v.role_id } : {}),
      });
      reset(EMPTY);
      onCreated(invitation, inviteLink(invitation));
      onOpenChange(false);
    } catch (err) {
      if (err instanceof ApiError && err.field && err.field in EMPTY) {
        setError(err.field as keyof FormValues, { message: err.message });
      } else {
        setError('root', { message: err instanceof ApiError ? err.message : 'Unable to create the invitation.' });
      }
    }
  });

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset(EMPTY);
        onOpenChange(next);
      }}
      title="Invite member"
      description="They'll get a one-time link to choose a password and sign in."
      footer={
        <>
          <Button type="button" variant="secondary" onClick={() => onOpenChange(false)} disabled={createInvitation.isPending}>
            Cancel
          </Button>
          <Button type="submit" form="invite-member-form" loading={createInvitation.isPending}>
            Invite
          </Button>
        </>
      }
    >
      <form id="invite-member-form" className="flex flex-col gap-4" onSubmit={onSubmit} noValidate>
        {errors.root?.message && <Alert variant="error">{errors.root.message}</Alert>}
        <FormField label="Name" htmlFor="invite-name" error={errors.name?.message} required>
          <Input id="invite-name" autoComplete="off" {...register('name')} />
        </FormField>
        <FormField label="Email" htmlFor="invite-email" error={errors.email?.message} required>
          <Input id="invite-email" type="email" autoComplete="off" {...register('email')} />
        </FormField>
        <FormField
          label="Role"
          htmlFor="invite-role"
          error={errors.role_id?.message}
          hint="Leave unset to invite them as a regular member."
        >
          <Controller
            control={control}
            name="role_id"
            render={({ field }) => (
              <Select
                id="invite-role"
                value={field.value}
                onValueChange={field.onChange}
                options={(roles ?? []).map((r) => ({ value: r.id, label: r.name }))}
                placeholder="Member (default)"
              />
            )}
          />
        </FormField>
      </form>
    </Dialog>
  );
}
