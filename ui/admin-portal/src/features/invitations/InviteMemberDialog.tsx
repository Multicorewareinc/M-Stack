import { zodResolver } from '@hookform/resolvers/zod';
import { Alert, Button, Dialog, FormField, Input, Select } from '@multistack/ui';
import { Controller, useForm } from 'react-hook-form';
import { z } from 'zod';
import { ApiError } from '../../api/client';
import { inviteLink, type Invitation, type InviteRole } from '../../api/invitations';
import { useOrganizations } from '../organizations/hooks/useOrganizations';
import { usePlansForSelect } from '../organizations/hooks/usePlansForSelect';
import { useCreateInvitation } from './hooks/useInvitations';

export const ROLE_LABELS: Record<InviteRole, string> = { org_admin: 'Organization admin', org_user: 'Member' };

const schema = z
  .object({
    name: z.string().trim().min(1, 'Name is required.'),
    email: z
    .string()
    .trim()
    .min(1, 'Email is required.')
    .email('Enter a valid email address.')
    // The server enforces the same limits (RFC 5321): 254 characters overall, 64 before the "@".
    .refine((v) => v.length <= 254 && v.split('@')[0].length <= 64, 'Enter a valid email address.'),
    target: z.enum(['existing', 'new']),
    organization_id: z.string(),
    new_org_name: z.string().trim(),
    plan_id: z.string(),
    role: z.enum(['org_admin', 'org_user']),
  })
  .superRefine((v, ctx) => {
    if (v.target === 'existing' && !v.organization_id) {
      ctx.addIssue({ code: 'custom', path: ['organization_id'], message: 'Choose an organization.' });
    }
    if (v.target === 'new') {
      if (!v.new_org_name) ctx.addIssue({ code: 'custom', path: ['new_org_name'], message: 'Organization name is required.' });
      if (!v.plan_id) ctx.addIssue({ code: 'custom', path: ['plan_id'], message: 'Choose a plan.' });
    }
  });
type FormValues = z.infer<typeof schema>;

const EMPTY: FormValues = {
  name: '',
  email: '',
  target: 'existing',
  organization_id: '',
  new_org_name: '',
  plan_id: '',
  role: 'org_user',
};

export interface InviteMemberDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (invitation: Invitation, link: string) => void;
}

/**
 * Super-admin invites someone by email. Pick an existing organization or create a new one for them
 * (a new organization's first user is normally its admin, so that role is preselected), then a role.
 * On success the dialog closes and the page shows the one-time link to copy: the invitee opens it in the
 * Organization Portal, chooses a password, and signs in. No email is sent.
 */
export function InviteMemberDialog({ open, onOpenChange, onCreated }: InviteMemberDialogProps) {
  const createInvitation = useCreateInvitation();
  const { data: organizations } = useOrganizations();
  const { data: plans } = usePlansForSelect();

  const {
    register,
    handleSubmit,
    control,
    setError,
    setValue,
    reset,
    watch,
    formState: { errors },
  } = useForm<FormValues>({ resolver: zodResolver(schema), defaultValues: EMPTY });

  const target = watch('target');

  const onSubmit = handleSubmit(async (v) => {
    try {
      const invitation = await createInvitation.mutateAsync({
        email: v.email,
        name: v.name,
        role: v.role,
        ...(v.target === 'existing'
          ? { organization_id: v.organization_id }
          : { new_organization: { name: v.new_org_name, plan_id: v.plan_id } }),
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
      <form id="invite-member-form" className="flex max-h-[60vh] flex-col gap-4 overflow-y-auto" onSubmit={onSubmit} noValidate>
        {errors.root?.message && <Alert variant="error">{errors.root.message}</Alert>}
        <FormField label="Name" htmlFor="invite-name" error={errors.name?.message} required>
          <Input id="invite-name" autoComplete="off" {...register('name')} />
        </FormField>
        <FormField label="Email" htmlFor="invite-email" error={errors.email?.message} required>
          <Input id="invite-email" type="email" autoComplete="off" {...register('email')} />
        </FormField>

        <FormField label="Organization" htmlFor="invite-target" required>
          <Controller
            control={control}
            name="target"
            render={({ field }) => (
              <Select
                id="invite-target"
                value={field.value}
                onValueChange={(next) => {
                  field.onChange(next);
                  // The first person in a brand-new organization is normally its admin.
                  setValue('role', next === 'new' ? 'org_admin' : 'org_user');
                }}
                options={[
                  { value: 'existing', label: 'Existing organization' },
                  { value: 'new', label: 'New organization' },
                ]}
              />
            )}
          />
        </FormField>

        {target === 'existing' ? (
          <FormField label="Select organization" htmlFor="invite-org" error={errors.organization_id?.message} required>
            <Controller
              control={control}
              name="organization_id"
              render={({ field }) => (
                <Select
                  id="invite-org"
                  aria-invalid={errors.organization_id ? true : undefined}
                  value={field.value}
                  onValueChange={field.onChange}
                  options={(organizations ?? []).map((o) => ({ value: o.id, label: o.name }))}
                  placeholder="Select an organization"
                />
              )}
            />
          </FormField>
        ) : (
          <>
            <FormField label="New organization name" htmlFor="invite-new-org" error={errors.new_org_name?.message} required>
              <Input id="invite-new-org" autoComplete="off" {...register('new_org_name')} />
            </FormField>
            <FormField label="Plan" htmlFor="invite-plan" error={errors.plan_id?.message} required>
              <Controller
                control={control}
                name="plan_id"
                render={({ field }) => (
                  <Select
                    id="invite-plan"
                    aria-invalid={errors.plan_id ? true : undefined}
                    value={field.value}
                    onValueChange={field.onChange}
                    options={(plans ?? []).filter((p) => p.is_active).map((p) => ({ value: p.id, label: p.name }))}
                    placeholder="Select a plan"
                  />
                )}
              />
            </FormField>
          </>
        )}

        <FormField label="Role" htmlFor="invite-role" error={errors.role?.message} required>
          <Controller
            control={control}
            name="role"
            render={({ field }) => (
              <Select
                id="invite-role"
                value={field.value}
                onValueChange={field.onChange}
                options={(Object.keys(ROLE_LABELS) as InviteRole[]).map((r) => ({ value: r, label: ROLE_LABELS[r] }))}
              />
            )}
          />
        </FormField>
      </form>
    </Dialog>
  );
}
