import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { ThemeProvider } from '@multistack/ui';
import { AppRoutes } from '../../app/routes';
import { ToastProvider } from '../../app/ToastProvider';
import { MockAuthProvider } from '../../auth/MockAuthProvider';
import { Sidebar } from '../../components/layout/Sidebar';
import { mockRoles } from '../../mocks/fixtures';
import { server } from '../../mocks/server';
import { InvitationsPage } from './InvitationsPage';

const DAY = 24 * 60 * 60 * 1000;
const TOKEN_PATH = '/accept-invite?token=org-invite-token-0123456789';

const row = (email: string, over: Record<string, unknown> = {}) => ({
  id: `inv_${email}`,
  email,
  name: null,
  organization_id: 'org_001',
  role_id: 'role_1',
  role_name: 'org_user',
  invited_by: 'admin@acme.com',
  created_at: new Date(Date.now() - DAY).toISOString(),
  expires_at: new Date(Date.now() + 3.5 * DAY).toISOString(),
  ...over,
});

const created = (over: Record<string, unknown> = {}) => ({
  id: 'inv_new',
  email: 'jane@example.com',
  name: 'Jane Doe',
  organization_id: 'org_001',
  organization_name: 'Acme Corp',
  role_id: 'role_1',
  role_name: 'org_user',
  expires_at: new Date(Date.now() + 7 * DAY).toISOString(),
  invite_path: TOKEN_PATH,
  invite_url: null,
  ...over,
});

/** Serves a mutable list on GET /api/invitations and records every request. */
function backend(initial: ReturnType<typeof row>[] = [row('alice@x.co'), row('bob@x.co', { role_name: 'org_admin' })]) {
  const state = { items: [...initial], gets: [] as URL[], deleted: [] as string[], posted: [] as Record<string, unknown>[] };
  server.use(
    http.get('/api/invitations', ({ request }) => {
      const url = new URL(request.url);
      state.gets.push(url);
      const limit = Number(url.searchParams.get('limit'));
      const offset = Number(url.searchParams.get('offset'));
      const search = url.searchParams.get('search')?.toLowerCase();
      const role = url.searchParams.get('role');
      const rows = state.items.filter((i) => (!search || i.email.includes(search)) && (!role || i.role_name === role));
      return HttpResponse.json({ items: rows.slice(offset, offset + limit), total: rows.length, limit, offset });
    }),
    http.delete('/api/invitations/:id', ({ params }) => {
      state.deleted.push(String(params.id));
      state.items = state.items.filter((i) => i.id !== params.id);
      return new HttpResponse(null, { status: 204 });
    }),
  );
  return state;
}

function renderPage(permissions?: string[]) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MockAuthProvider permissions={permissions}>
        <ToastProvider>
          <MemoryRouter>
            <InvitationsPage />
          </MemoryRouter>
        </ToastProvider>
      </MockAuthProvider>
    </QueryClientProvider>,
  );
}

const table = () => within(screen.getByRole('table'));
const lastGet = (s: ReturnType<typeof backend>) => s.gets[s.gets.length - 1];

describe('InvitationsPage layout', () => {
  it('is a page with a header, an Invite member button, a search card and the pending list', async () => {
    backend();
    renderPage();
    expect(screen.getByRole('heading', { name: 'Invitations' })).toBeInTheDocument();
    expect(screen.getByText('Invite people to your organization and manage pending invitations.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Invite member' })).toBeInTheDocument();
    expect(screen.getByRole('form', { name: 'Search invitations' })).toBeInTheDocument();
    for (const h of ['Email', 'Role', 'Created', 'Expires']) {
      expect(await screen.findByRole('columnheader', { name: h })).toBeInTheDocument();
    }
    expect(await table().findByText('alice@x.co')).toBeInTheDocument();
    expect(table().getByText('bob@x.co')).toBeInTheDocument();
    expect(table().getByText('org_admin')).toBeInTheDocument();
    expect(table().getAllByText(/^in 3d · /)).toHaveLength(2); // "in 3d · 26 Sep 2026"
    expect(screen.getByText('Showing 1–2 of 2')).toBeInTheDocument();
    expect(screen.queryByText('Invite link created')).not.toBeInTheDocument();
  });

  it('flags an expired invitation and shows an empty state when there are none', async () => {
    backend([row('old@x.co', { expires_at: new Date(Date.now() - DAY).toISOString() })]);
    const { unmount } = renderPage();
    expect(await table().findByText(/^Expired · /)).toBeInTheDocument();
    unmount();

    backend([]);
    renderPage();
    expect(await screen.findByText('No pending invitations')).toBeInTheDocument();
    expect(screen.getByText('Invite someone to get started.')).toBeInTheDocument();
  });

  it('shows the API error with a retry', async () => {
    server.use(
      http.get('/api/invitations', () =>
        HttpResponse.json({ error: { message: 'Forbidden', type: 'forbidden' } }, { status: 403 }),
      ),
    );
    renderPage();
    expect(await screen.findByText('Forbidden')).toBeInTheDocument();
  });
});

describe('InvitationsPage search', () => {
  it('searches by email on the Search button and on Enter, returning to page 1', async () => {
    const s = backend();
    renderPage();
    await table().findByText('alice@x.co');
    expect(lastGet(s).searchParams.get('search')).toBeNull();

    await userEvent.type(screen.getByLabelText('Email'), 'ALI');
    await userEvent.click(screen.getByRole('button', { name: 'Search' }));
    await waitFor(() => expect(lastGet(s).searchParams.get('search')).toBe('ALI'));
    expect(lastGet(s).searchParams.get('offset')).toBe('0');
    await waitFor(() => expect(table().queryByText('bob@x.co')).not.toBeInTheDocument());
    expect(table().getByText('alice@x.co')).toBeInTheDocument();

    await userEvent.clear(screen.getByLabelText('Email'));
    await userEvent.type(screen.getByLabelText('Email'), 'bob{Enter}');
    await waitFor(() => expect(lastGet(s).searchParams.get('search')).toBe('bob'));
  });

  it('filters by role', async () => {
    const s = backend();
    renderPage();
    await table().findByText('alice@x.co');
    const role = screen.getByLabelText('Role');
    await waitFor(() => expect(within(role).getAllByRole('option').length).toBeGreaterThan(1));
    await userEvent.selectOptions(role, mockRoles[0].name);
    await waitFor(() => expect(lastGet(s).searchParams.get('role')).toBe(mockRoles[0].name));
    await userEvent.selectOptions(role, 'All roles');
    await waitFor(() => expect(lastGet(s).searchParams.get('role')).toBeNull());
  });

  it('pages through the list', async () => {
    const many = Array.from({ length: 12 }, (_, n) => row(`user${String(n).padStart(2, '0')}@x.co`));
    const s = backend(many);
    renderPage();
    await table().findByText('user00@x.co');
    expect(screen.getByText('Showing 1–10 of 12')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(await table().findByText('user10@x.co')).toBeInTheDocument();
    expect(screen.getByText('Showing 11–12 of 12')).toBeInTheDocument();
    expect(lastGet(s).searchParams.get('offset')).toBe('10');
  });
});

describe('Inviting a member', () => {
  it('opens a dialog (not a side drawer), then shows the link card and refreshes the list', async () => {
    const s = backend();
    server.use(
      http.post('/api/invitations', async ({ request }) => {
        s.posted.push((await request.json()) as Record<string, unknown>);
        s.items = [row('jane@example.com'), ...s.items];
        return HttpResponse.json(created(), { status: 201 });
      }),
    );
    renderPage();
    await table().findByText('alice@x.co');

    await userEvent.click(screen.getByRole('button', { name: 'Invite member' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Invite member')).toBeInTheDocument();
    expect(within(dialog).queryByLabelText('Organization')).not.toBeInTheDocument(); // always their own org
    await userEvent.type(within(dialog).getByLabelText('Name'), 'Jane Doe');
    await userEvent.type(within(dialog).getByLabelText('Email'), 'jane@example.com');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Invite' }));

    expect(await screen.findByText('Invite link created')).toBeInTheDocument();
    expect(screen.getByText("Copy this link now — it won't be shown again.")).toBeInTheDocument();
    expect(((await screen.findByLabelText('Invitation link')) as HTMLInputElement).value).toBe(
      `${window.location.origin}${TOKEN_PATH}`,
    );
    expect(screen.getByText('jane@example.com', { selector: 'span' })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(s.posted[0]).toEqual({ email: 'jane@example.com', name: 'Jane Doe' });
    // the new invitation is in the list without a manual refresh
    expect(await table().findByText('jane@example.com')).toBeInTheDocument();
  });

  it('copies the link and can be dismissed', async () => {
    const user = userEvent.setup();
    backend();
    server.use(http.post('/api/invitations', () => HttpResponse.json(created(), { status: 201 })));
    renderPage();
    await user.click(screen.getByRole('button', { name: 'Invite member' }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText('Name'), 'Jane');
    await user.type(within(dialog).getByLabelText('Email'), 'jane@example.com');
    await user.click(within(dialog).getByRole('button', { name: 'Invite' }));
    const link = ((await screen.findByLabelText('Invitation link')) as HTMLInputElement).value;

    await user.click(screen.getByRole('button', { name: 'Copy link' }));
    expect(await screen.findByRole('button', { name: 'Copied' })).toBeInTheDocument();
    expect(await navigator.clipboard.readText()).toBe(link);

    await user.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(screen.queryByText('Invite link created')).not.toBeInTheDocument();
  });

  it('keeps the dialog open and flags the email when it already has an account', async () => {
    backend();
    server.use(
      http.post('/api/invitations', () =>
        HttpResponse.json(
          { error: { message: 'A user with this email already exists', type: 'conflict', field: 'email' } },
          { status: 409 },
        ),
      ),
    );
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: 'Invite member' }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText('Name'), 'Jane');
    await userEvent.type(within(dialog).getByLabelText('Email'), 'jane@example.com');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Invite' }));
    expect(await within(dialog).findByText('A user with this email already exists')).toBeInTheDocument();
    expect(screen.queryByText('Invite link created')).not.toBeInTheDocument();
  });

  it('rejects malformed email addresses on the form, before anything is sent', async () => {
    const s = backend();
    const posted: unknown[] = [];
    server.use(
      http.post('/api/invitations', async ({ request }) => {
        posted.push(await request.json());
        return HttpResponse.json(created(), { status: 201 });
      }),
    );
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: 'Invite member' }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText('Name'), 'Jane');
    for (const bad of ['jane@example', 'a..b@example.com', 'x@y.1', `${'a'.repeat(65)}@example.com`, 'jane doe@example.com']) {
      await userEvent.clear(within(dialog).getByLabelText('Email'));
      await userEvent.type(within(dialog).getByLabelText('Email'), bad);
      await userEvent.click(within(dialog).getByRole('button', { name: 'Invite' }));
      expect(await within(dialog).findByText('Enter a valid email address.')).toBeInTheDocument();
    }
    expect(posted).toHaveLength(0);
    expect(s.gets.length).toBeGreaterThan(0);
  }, 20_000);

  it('validates name and email before sending anything', async () => {
    backend();
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: 'Invite member' }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText('Email'), 'nope');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Invite' }));
    expect(await within(dialog).findByText('Name is required.')).toBeInTheDocument();
    expect(within(dialog).getByText('Enter a valid email address.')).toBeInTheDocument();
  });
});

describe('Revoking an invitation', () => {
  it('asks first, then revokes and refreshes the list', async () => {
    const s = backend();
    renderPage();
    await table().findByText('alice@x.co');
    await userEvent.click(screen.getByRole('button', { name: 'Revoke invitation for alice@x.co' }));
    const confirm = await screen.findByRole('dialog');
    expect(within(confirm).getByText('Revoke invitation?')).toBeInTheDocument();
    expect(s.deleted).toHaveLength(0); // nothing happens until confirmed
    await userEvent.click(within(confirm).getByRole('button', { name: 'Revoke' }));

    await waitFor(() => expect(s.deleted).toEqual(['inv_alice@x.co']));
    await waitFor(() => expect(table().queryByText('alice@x.co')).not.toBeInTheDocument());
    expect(table().getByText('bob@x.co')).toBeInTheDocument();
  });

  it('cancelling revokes nothing', async () => {
    const s = backend();
    renderPage();
    await table().findByText('alice@x.co');
    await userEvent.click(screen.getByRole('button', { name: 'Revoke invitation for alice@x.co' }));
    await userEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(s.deleted).toHaveLength(0);
    expect(table().getByText('alice@x.co')).toBeInTheDocument();
  });

  it('keeps the row and says so when the server refuses', async () => {
    backend();
    server.use(
      http.delete('/api/invitations/:id', () =>
        HttpResponse.json({ error: { message: 'This invitation was already accepted', type: 'conflict' } }, { status: 409 }),
      ),
    );
    renderPage();
    await table().findByText('alice@x.co');
    await userEvent.click(screen.getByRole('button', { name: 'Revoke invitation for alice@x.co' }));
    await userEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Revoke' }));
    expect(await screen.findByText('This invitation was already accepted')).toBeInTheDocument();
    expect(table().getByText('alice@x.co')).toBeInTheDocument();
  });
});

describe('Invitations in the navigation', () => {
  function renderApp(permissions: string[], path = '/invitations') {
    backend();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <ThemeProvider>
        <QueryClientProvider client={queryClient}>
          <MockAuthProvider permissions={permissions}>
            <ToastProvider>
              <MemoryRouter initialEntries={[path]}>
                <AppRoutes />
              </MemoryRouter>
            </ToastProvider>
          </MockAuthProvider>
        </QueryClientProvider>
      </ThemeProvider>,
    );
  }

  it('is a regular sidebar link like the others, not a button', () => {
    const queryClient = new QueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <MockAuthProvider permissions={['users.create']}>
          <MemoryRouter>
            <Sidebar />
          </MemoryRouter>
        </MockAuthProvider>
      </QueryClientProvider>,
    );
    expect(screen.getByRole('link', { name: 'Invitations' })).toHaveAttribute('href', '/invitations');
    expect(screen.queryByRole('button', { name: /^Invite/ })).not.toBeInTheDocument();
  });

  it('is hidden without users.create, and the page is not reachable either', async () => {
    const queryClient = new QueryClient();
    const { unmount } = render(
      <QueryClientProvider client={queryClient}>
        <MockAuthProvider permissions={['users.read']}>
          <MemoryRouter>
            <Sidebar />
          </MemoryRouter>
        </MockAuthProvider>
      </QueryClientProvider>,
    );
    expect(screen.queryByRole('link', { name: 'Invitations' })).not.toBeInTheDocument();
    unmount();

    renderApp(['users.read']);
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Invitations' })).not.toBeInTheDocument());
  });

  it('renders at /invitations for someone who can create users', async () => {
    renderApp(['users.create']);
    expect(await screen.findByRole('heading', { name: 'Invitations' })).toBeInTheDocument();
    expect(await table().findByText('alice@x.co')).toBeInTheDocument();
  });
});

describe('Email delivery of a new invitation', () => {
  async function inviteAndGetCard(status: 'sent' | 'failed' | 'not_configured' | 'rejected', detail: string | null = null) {
    const s = backend();
    server.use(
      http.post('/api/invitations', async () =>
        HttpResponse.json(
          // like the real API: no link comes back once the email has gone out
          created({ email_status: status, email_detail: detail, ...(status === 'sent' ? { invite_path: null } : {}) }),
          { status: 201 },
        ),
      ),
    );
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: 'Invite member' }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText('Name'), 'Jane Doe');
    await userEvent.type(within(dialog).getByLabelText('Email'), 'jane@example.com');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Invite' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    return s;
  }

  it('just confirms when the invitation was emailed: no link card, nothing to copy', async () => {
    await inviteAndGetCard('sent');
    expect(await screen.findByText('Invitation sent')).toBeInTheDocument();
    expect(screen.getByText('We emailed the invitation to jane@example.com.')).toBeInTheDocument();
    expect(screen.queryByText('Invite link created')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Invitation link')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Copy link' })).not.toBeInTheDocument();
  });

  it('warns, with the reason, when the email could not be sent', async () => {
    await inviteAndGetCard('failed', 'SMTPAuthenticationError: (535, Username and Password not accepted)');
    expect(await screen.findByText('Invite link created')).toBeInTheDocument(); // the fallback: link stays on screen
    expect(screen.getByLabelText('Invitation link')).toBeInTheDocument();
    expect(screen.getByText('The email could not be sent')).toBeInTheDocument();
    expect(screen.getByText(/SMTPAuthenticationError/)).toBeInTheDocument();
    expect(screen.getByText(/send it to jane@example.com yourself/)).toBeInTheDocument();
  });

  it('says the address was rejected, with the mail server\'s reason, and keeps the link', async () => {
    await inviteAndGetCard('rejected', '550 5.1.1 The email account that you tried to reach does not exist.');
    expect(await screen.findByText('That email address was rejected')).toBeInTheDocument();
    expect(screen.getByText(/can't receive mail/)).toBeInTheDocument();
    expect(screen.getByText(/does not exist/)).toBeInTheDocument();
    expect(screen.getByText(/Check the address for typos/)).toBeInTheDocument();
    expect(screen.getByLabelText('Invitation link')).toBeInTheDocument();
    expect(screen.queryByText('Invitation sent')).not.toBeInTheDocument();
  });

  it('tells the admin to send the link themselves when email is not set up', async () => {
    await inviteAndGetCard('not_configured', 'SMTP is not configured (SMTP_HOST and SMTP_FROM_ADDRESS).');
    expect(await screen.findByText('Invite link created')).toBeInTheDocument();
    expect(screen.getByLabelText('Invitation link')).toBeInTheDocument();
    expect(screen.getByText(/Email isn.t set up, so nothing was sent/)).toBeInTheDocument();
    expect(screen.queryByText(/We emailed this link/)).not.toBeInTheDocument();
  });
});
