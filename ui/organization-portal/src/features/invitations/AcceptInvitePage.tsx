import { Alert, BrandLogo, Button, FormField, Input, Spinner } from '@multistack/ui';
import * as React from 'react';
import { ApiError } from '../../api/client';
import { acceptInvitation, inspectInvitation, type InvitationDetails } from '../../api/invitations';
import { BRANDING } from '../../branding';

// Matches organization-control-plane's password policy (settings.password_min_length).
const MIN_PASSWORD_LENGTH = 12;

export const ACCEPT_INVITE_PATH = '/accept-invite';

/** True on the public invitation link. Checked by <App/> BEFORE any auth provider mounts: the invitee
 * has no account yet, so they must reach this page without being sent to the sign-in screen. */
export function isAcceptInviteRoute(pathname = window.location.pathname): boolean {
  return pathname.replace(/\/+$/, '') === ACCEPT_INVITE_PATH;
}

type View =
  | { kind: 'loading' }
  | { kind: 'invalid'; message: string }
  | { kind: 'ready'; details: InvitationDetails }
  | { kind: 'done' };

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-neutral-50 p-4">
      <div className="flex w-full max-w-sm flex-col gap-4 rounded-lg border border-subtle bg-neutral-0 p-6 shadow-md">
        <BrandLogo name={BRANDING.appName} logoUrl={BRANDING.logoUrl} logoHeight={28} />
        {children}
      </div>
    </div>
  );
}

/** Public page behind an invitation link: confirms who the invitation is for, lets them choose a
 * password, then sends them to sign in. Uses plain fetches (no session exists yet). */
export function AcceptInvitePage() {
  const token = React.useMemo(() => new URLSearchParams(window.location.search).get('token') ?? '', []);
  const [view, setView] = React.useState<View>(
    token ? { kind: 'loading' } : { kind: 'invalid', message: 'This invitation link is not valid.' },
  );
  const [password, setPassword] = React.useState('');
  const [confirm, setConfirm] = React.useState('');
  const [error, setError] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);

  React.useEffect(() => {
    if (!token) return;
    let cancelled = false;
    inspectInvitation(token)
      .then((details) => !cancelled && setView({ kind: 'ready', details }))
      .catch((err) => {
        if (cancelled) return;
        setView({
          kind: 'invalid',
          message: err instanceof ApiError ? err.message : 'We could not check this invitation. Try again later.',
        });
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (password.length < MIN_PASSWORD_LENGTH) {
      setError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }
    if (password !== confirm) {
      setError('The two passwords do not match.');
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      await acceptInvitation(token, password);
      setView({ kind: 'done' });
    } catch (err) {
      if (err instanceof ApiError && err.status === 422) {
        setError(err.message); // the server's password-policy message — stay on the form
      } else if (err instanceof ApiError && (err.status === 404 || err.status === 410 || err.status === 409)) {
        setView({ kind: 'invalid', message: err.message });
      } else {
        setError(err instanceof ApiError ? err.message : 'Unable to set your password. Try again.');
      }
    } finally {
      setSubmitting(false);
    }
  }

  if (view.kind === 'loading') {
    return (
      <Shell>
        <div className="flex justify-center py-6">
          <Spinner label="Checking invitation" />
        </div>
      </Shell>
    );
  }

  if (view.kind === 'invalid') {
    return (
      <Shell>
        <h1 className="text-pageTitle font-semibold text-neutral-900">Invitation unavailable</h1>
        <Alert variant="error">{view.message}</Alert>
        <p className="text-secondary text-neutral-500">Ask the person who invited you to send a new link.</p>
      </Shell>
    );
  }

  if (view.kind === 'done') {
    return (
      <Shell>
        <h1 className="text-pageTitle font-semibold text-neutral-900">You&apos;re all set</h1>
        <Alert variant="success">Your password is saved. Sign in to continue.</Alert>
        <Button onClick={() => window.location.assign('/')}>Go to sign in</Button>
      </Shell>
    );
  }

  const { details } = view;
  return (
    <Shell>
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <div className="flex flex-col gap-1">
          <h1 className="text-pageTitle font-semibold text-neutral-900">Accept your invitation</h1>
          <p className="text-secondary text-neutral-500">
            {details.name ? `${details.name}, you've` : "You've"} been invited to join{' '}
            <strong className="text-neutral-900">{details.organization_name}</strong> as{' '}
            <strong className="text-neutral-900">{details.role_name}</strong>. Choose a password to finish.
          </p>
        </div>
        {error && <Alert variant="error">{error}</Alert>}
        <FormField label="Email" htmlFor="invite-email">
          <Input id="invite-email" type="email" value={details.email} readOnly />
        </FormField>
        <FormField
          label="Password"
          htmlFor="invite-password"
          required
          hint={`At least ${MIN_PASSWORD_LENGTH} characters.`}
        >
          <Input
            id="invite-password"
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </FormField>
        <FormField label="Confirm password" htmlFor="invite-confirm" required>
          <Input
            id="invite-confirm"
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            required
          />
        </FormField>
        <Button type="submit" loading={submitting} disabled={submitting}>
          Set password
        </Button>
      </form>
    </Shell>
  );
}
