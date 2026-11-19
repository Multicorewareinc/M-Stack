import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterEach, describe, expect, it } from 'vitest';
import { App } from '../../App';
import { server } from '../../mocks/server';
import { AcceptInvitePage, isAcceptInviteRoute } from './AcceptInvitePage';

const TOKEN = 'a-valid-looking-invitation-token-0123456789';
const DETAILS = {
  email: 'jane@example.com',
  name: 'Jane Doe',
  organization_name: 'Acme Corp',
  role_name: 'org_admin',
  expires_at: '2026-12-31T00:00:00Z',
};

function openLink(search = `?token=${TOKEN}`) {
  window.history.pushState({}, '', `/accept-invite${search}`);
}

afterEach(() => window.history.pushState({}, '', '/'));

function stub(inspect: () => Response, accept?: (body: unknown) => Response) {
  const accepted: unknown[] = [];
  server.use(
    http.post('/api/invitations/inspect', inspect),
    http.post('/api/invitations/accept', async ({ request }) => {
      const body = await request.json();
      accepted.push(body);
      return accept ? accept(body) : new HttpResponse(null, { status: 204 });
    }),
  );
  return accepted;
}

describe('AcceptInvitePage', () => {
  it('shows who the invitation is for and lets them set a password', async () => {
    openLink();
    const accepted = stub(() => HttpResponse.json(DETAILS));
    render(<AcceptInvitePage />);

    expect(await screen.findByText('Accept your invitation')).toBeInTheDocument();
    expect(screen.getByText('Acme Corp')).toBeInTheDocument();
    expect(screen.getByText('org_admin')).toBeInTheDocument();
    expect(screen.getByLabelText('Email')).toHaveValue('jane@example.com');
    expect(screen.getByLabelText('Email')).toHaveAttribute('readonly');

    await userEvent.type(screen.getByLabelText('Password'), 'Chosen-By-Invitee-42!');
    await userEvent.type(screen.getByLabelText('Confirm password'), 'Chosen-By-Invitee-42!');
    await userEvent.click(screen.getByRole('button', { name: 'Set password' }));

    expect(await screen.findByText('You\'re all set')).toBeInTheDocument();
    expect(accepted).toEqual([{ token: TOKEN, password: 'Chosen-By-Invitee-42!' }]);
    expect(screen.getByRole('button', { name: 'Go to sign in' })).toBeInTheDocument();
  });

  it('checks the password length and confirmation before calling the server', async () => {
    openLink();
    const accepted = stub(() => HttpResponse.json(DETAILS));
    render(<AcceptInvitePage />);
    await screen.findByText('Accept your invitation');

    await userEvent.type(screen.getByLabelText('Password'), 'short');
    await userEvent.type(screen.getByLabelText('Confirm password'), 'short');
    await userEvent.click(screen.getByRole('button', { name: 'Set password' }));
    expect(await screen.findByText('Password must be at least 12 characters.')).toBeInTheDocument();

    await userEvent.clear(screen.getByLabelText('Password'));
    await userEvent.clear(screen.getByLabelText('Confirm password'));
    await userEvent.type(screen.getByLabelText('Password'), 'Long-enough-password-1');
    await userEvent.type(screen.getByLabelText('Confirm password'), 'Different-password-12');
    await userEvent.click(screen.getByRole('button', { name: 'Set password' }));
    expect(await screen.findByText('The two passwords do not match.')).toBeInTheDocument();
    expect(accepted).toHaveLength(0);
  });

  it('stays on the form and shows the server’s password-policy message', async () => {
    openLink();
    stub(
      () => HttpResponse.json(DETAILS),
      () => HttpResponse.json({ error: { message: 'Password is too common.', type: 'unprocessable' } }, { status: 422 }),
    );
    render(<AcceptInvitePage />);
    await screen.findByText('Accept your invitation');
    await userEvent.type(screen.getByLabelText('Password'), 'Password1234!!');
    await userEvent.type(screen.getByLabelText('Confirm password'), 'Password1234!!');
    await userEvent.click(screen.getByRole('button', { name: 'Set password' }));
    expect(await screen.findByText('Password is too common.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Set password' })).toBeInTheDocument();
  });

  it('says so when the link has expired or was already used', async () => {
    openLink();
    stub(() =>
      HttpResponse.json(
        { error: { message: 'This invitation has expired. Ask for a new link.', type: 'gone' } },
        { status: 410 },
      ),
    );
    render(<AcceptInvitePage />);
    expect(await screen.findByText('Invitation unavailable')).toBeInTheDocument();
    expect(screen.getByText('This invitation has expired. Ask for a new link.')).toBeInTheDocument();
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument();
  });

  it('rejects an unknown token and a link with no token at all', async () => {
    openLink();
    stub(() =>
      HttpResponse.json({ error: { message: 'This invitation link is not valid.', type: 'not_found' } }, { status: 404 }),
    );
    const { unmount } = render(<AcceptInvitePage />);
    expect(await screen.findByText('This invitation link is not valid.')).toBeInTheDocument();
    unmount();

    openLink('');
    render(<AcceptInvitePage />);
    expect(screen.getByText('Invitation unavailable')).toBeInTheDocument();
  });

  it('moves to "unavailable" if the link is used up between loading and submitting', async () => {
    openLink();
    stub(
      () => HttpResponse.json(DETAILS),
      () =>
        HttpResponse.json(
          { error: { message: 'This invitation has already been used or replaced. Ask for a new link.', type: 'gone' } },
          { status: 410 },
        ),
    );
    render(<AcceptInvitePage />);
    await screen.findByText('Accept your invitation');
    await userEvent.type(screen.getByLabelText('Password'), 'Chosen-By-Invitee-42!');
    await userEvent.type(screen.getByLabelText('Confirm password'), 'Chosen-By-Invitee-42!');
    await userEvent.click(screen.getByRole('button', { name: 'Set password' }));
    expect(await screen.findByText('Invitation unavailable')).toBeInTheDocument();
  });
});

describe('invitation route', () => {
  it('is recognised with or without a trailing slash', () => {
    expect(isAcceptInviteRoute('/accept-invite')).toBe(true);
    expect(isAcceptInviteRoute('/accept-invite/')).toBe(true);
    expect(isAcceptInviteRoute('/users')).toBe(false);
    expect(isAcceptInviteRoute('/')).toBe(false);
  });

  it('renders for someone who is not signed in, instead of the app or the sign-in screen', async () => {
    openLink();
    stub(() => HttpResponse.json(DETAILS));
    render(<App />);
    expect(await screen.findByText('Accept your invitation')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('navigation', { name: 'Primary' })).not.toBeInTheDocument());
    expect(screen.queryByText('Sign in')).not.toBeInTheDocument();
  });
});
