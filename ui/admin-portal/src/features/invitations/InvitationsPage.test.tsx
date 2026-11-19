import { ThemeProvider } from '@multistack/ui';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { AppRoutes } from '../../app/routes';
import { ToastProvider } from '../../app/ToastProvider';
import { MockAuthProvider } from '../../auth/MockAuthProvider';
import { Sidebar } from '../../components/layout/Sidebar';
import { mockOrganizations, mockPlans } from '../../mocks/fixtures';
import { server } from '../../mocks/server';
import { InvitationsPage } from './InvitationsPage';

const ACME = mockOrganizations[0]; // seeded invitation sam@acme.com (org_admin)
const GLOBEX = mockOrganizations[1]; // seeded invitation priya@globex.com (org_user)
const TOKEN_PATH = '/accept-invite?token=mock-invite-token-0123456789';

function renderWith(ui: React.ReactElement, path = '/') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <MockAuthProvider>
          <ToastProvider>
            <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
          </ToastProvider>
        </MockAuthProvider>
      </QueryClientProvider>
    </ThemeProvider>,
  );
}

const renderPage = () => renderWith(<InvitationsPage />);
const table = () => within(screen.getByRole('table'));

/** Records the query string of every GET and the body of every POST/DELETE on /v1/invitations. */
function spy() {
  const s = { gets: [] as URL[], posts: [] as Record<string, unknown>[], deletes: [] as string[] };
  server.events.on('request:start', async ({ request }) => {
    const url = new URL(request.url);
    if (!url.pathname.startsWith('/v1/invitations')) return;
    if (request.method === 'GET') s.gets.push(url);
    if (request.method === 'POST') s.posts.push(await request.clone().json());
    if (request.method === 'DELETE') s.deletes.push(url.pathname.split('/').pop() as string);
  });
  return s;
}
const lastGet = (s: ReturnType<typeof spy>) => s.gets[s.gets.length - 1];

async function waitForOrgNames() {
  await waitFor(() => expect(table().getByText(ACME.name)).toBeInTheDocument());
}

describe('Invitations page (admin)', () => {
  it('lists pending invitations across organizations, with organization names', async () => {
    renderPage();
    expect(screen.getByRole('heading', { name: 'Invitations' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Invite member' })).toBeInTheDocument();
    expect(screen.getByRole('form', { name: 'Search invitations' })).toBeInTheDocument();
    for (const h of ['Email', 'Organization', 'Role', 'Created', 'Expires']) {
      expect(await screen.findByRole('columnheader', { name: h })).toBeInTheDocument();
    }
    expect(await table().findByText('sam@acme.com')).toBeInTheDocument();
    expect(table().getByText('priya@globex.com')).toBeInTheDocument();
    await waitForOrgNames();
    expect(table().getByText(GLOBEX.name)).toBeInTheDocument();
    expect(table().getByText('org_admin')).toBeInTheDocument();
    expect(table().getByText(/^in 3d · /)).toBeInTheDocument();
    expect(screen.getByText('Showing 1–2 of 2')).toBeInTheDocument();
  });

  it('searches by email, and filters by organization and role, returning to page 1', async () => {
    const s = spy();
    renderPage();
    await table().findByText('sam@acme.com');

    await userEvent.type(screen.getByLabelText('Email'), 'PRIYA');
    await userEvent.click(screen.getByRole('button', { name: 'Search' }));
    await waitFor(() => expect(lastGet(s).searchParams.get('search')).toBe('PRIYA'));
    await waitFor(() => expect(table().queryByText('sam@acme.com')).not.toBeInTheDocument());
    expect(table().getByText('priya@globex.com')).toBeInTheDocument();

    await userEvent.clear(screen.getByLabelText('Email'));
    await userEvent.click(screen.getByRole('button', { name: 'Search' }));
    await table().findByText('sam@acme.com');

    const orgSelect = screen.getByLabelText('Organization');
    await waitFor(() => expect(within(orgSelect).getAllByRole('option').length).toBeGreaterThan(1));
    await userEvent.selectOptions(orgSelect, ACME.name);
    await waitFor(() => expect(lastGet(s).searchParams.get('organization_id')).toBe(ACME.id));
    expect(lastGet(s).searchParams.get('offset')).toBe('0');
    await waitFor(() => expect(table().queryByText('priya@globex.com')).not.toBeInTheDocument());

    await userEvent.selectOptions(screen.getByLabelText('Organization'), 'All organizations');
    await userEvent.selectOptions(screen.getByLabelText('Role'), 'org_user');
    await waitFor(() => expect(lastGet(s).searchParams.get('role')).toBe('org_user'));
    await waitFor(() => expect(table().queryByText('sam@acme.com')).not.toBeInTheDocument());
    expect(table().getByText('priya@globex.com')).toBeInTheDocument();
  });

  it('shows an empty state for no matches and the API error otherwise', async () => {
    renderPage();
    await table().findByText('sam@acme.com');
    await userEvent.type(screen.getByLabelText('Email'), 'nobody-like-this');
    await userEvent.click(screen.getByRole('button', { name: 'Search' }));
    expect(await screen.findByText('No pending invitations')).toBeInTheDocument();
    expect(screen.getByText('Nothing matches these filters.')).toBeInTheDocument();

    server.use(
      http.get('/v1/invitations', () =>
        HttpResponse.json({ error: { message: 'Org CP unavailable', type: 'upstream_error' } }, { status: 502 }),
      ),
    );
    renderPage();
    expect(await screen.findByText('Org CP unavailable')).toBeInTheDocument();
  });
});

describe('Inviting a member (admin)', () => {
  it('opens a dialog, invites into an existing org, shows the link card, and lists it', async () => {
    const s = spy();
    renderPage();
    await table().findByText('sam@acme.com');

    await userEvent.click(screen.getByRole('button', { name: 'Invite member' }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText('Name'), 'Jane Doe');
    await userEvent.type(within(dialog).getByLabelText('Email'), 'jane@example.com');
    const orgs = within(dialog).getByLabelText('Select organization');
    await waitFor(() => expect(within(orgs).getAllByRole('option').length).toBeGreaterThan(1));
    await userEvent.selectOptions(orgs, GLOBEX.name);
    await userEvent.click(within(dialog).getByRole('button', { name: 'Invite' }));

    expect(await screen.findByText('Invite link created')).toBeInTheDocument();
    const link = screen.getByLabelText('Invitation link') as HTMLInputElement;
    expect(link.value.endsWith(TOKEN_PATH)).toBe(true);
    expect(screen.getByText(GLOBEX.name, { selector: 'span.font-medium' })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(s.posts[0]).toEqual({
      email: 'jane@example.com',
      name: 'Jane Doe',
      role: 'org_user',
      organization_id: GLOBEX.id,
    });
    expect(await table().findByText('jane@example.com')).toBeInTheDocument();
  });

  it('creates a new organization for the invitee, who defaults to org admin', async () => {
    const s = spy();
    renderPage();
    await table().findByText('sam@acme.com');
    await userEvent.click(screen.getByRole('button', { name: 'Invite member' }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText('Name'), 'Ravi Kumar');
    await userEvent.type(within(dialog).getByLabelText('Email'), 'ravi@newco.com');
    await userEvent.selectOptions(within(dialog).getByLabelText('Organization'), 'New organization');
    expect(within(dialog).queryByLabelText('Select organization')).not.toBeInTheDocument();
    expect(within(dialog).getByLabelText('Role')).toHaveValue('org_admin');
    await userEvent.type(within(dialog).getByLabelText('New organization name'), 'NewCo');
    await userEvent.selectOptions(await within(dialog).findByLabelText('Plan'), mockPlans[0].name);
    await userEvent.click(within(dialog).getByRole('button', { name: 'Invite' }));

    expect(await screen.findByText('Invite link created')).toBeInTheDocument();
    expect(screen.getByText('NewCo (new)')).toBeInTheDocument();
    expect(s.posts[0]).toEqual({
      email: 'ravi@newco.com',
      name: 'Ravi Kumar',
      role: 'org_admin',
      new_organization: { name: 'NewCo', plan_id: mockPlans[0].id },
    });
  });

  it('copies the link and can be dismissed', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole('button', { name: 'Invite member' }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText('Name'), 'Jane');
    await user.type(within(dialog).getByLabelText('Email'), 'jane@example.com');
    const orgs = within(dialog).getByLabelText('Select organization');
    await waitFor(() => expect(within(orgs).getAllByRole('option').length).toBeGreaterThan(1));
    await user.selectOptions(orgs, ACME.name);
    await user.click(within(dialog).getByRole('button', { name: 'Invite' }));
    const link = ((await screen.findByLabelText('Invitation link')) as HTMLInputElement).value;
    await user.click(screen.getByRole('button', { name: 'Copy link' }));
    expect(await screen.findByRole('button', { name: 'Copied' })).toBeInTheDocument();
    expect(await navigator.clipboard.readText()).toBe(link);
    await user.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(screen.queryByText('Invite link created')).not.toBeInTheDocument();
  });

  it('rejects malformed email addresses on the form, before anything is sent', async () => {
    const s = spy();
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
    expect(s.posts).toHaveLength(0);
  }, 20_000);

  it('validates the form and reports an email that already has an account', async () => {
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: 'Invite member' }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText('Email'), 'nope');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Invite' }));
    expect(await within(dialog).findByText('Name is required.')).toBeInTheDocument();
    expect(within(dialog).getByText('Enter a valid email address.')).toBeInTheDocument();
    expect(within(dialog).getByText('Choose an organization.')).toBeInTheDocument();

    await userEvent.selectOptions(within(dialog).getByLabelText('Organization'), 'New organization');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Invite' }));
    expect(await within(dialog).findByText('Organization name is required.')).toBeInTheDocument();
    expect(within(dialog).getByText('Choose a plan.')).toBeInTheDocument();

    await userEvent.selectOptions(within(dialog).getByLabelText('Organization'), 'Existing organization');
    await userEvent.clear(within(dialog).getByLabelText('Email'));
    await userEvent.type(within(dialog).getByLabelText('Email'), 'taken@example.com');
    await userEvent.type(within(dialog).getByLabelText('Name'), 'Taken');
    const orgs = within(dialog).getByLabelText('Select organization');
    await waitFor(() => expect(within(orgs).getAllByRole('option').length).toBeGreaterThan(1));
    await userEvent.selectOptions(orgs, ACME.name);
    await userEvent.click(within(dialog).getByRole('button', { name: 'Invite' }));
    expect(await within(dialog).findByText('A user with this email already exists')).toBeInTheDocument();
    expect(screen.queryByText('Invite link created')).not.toBeInTheDocument();
  });
});

describe('Revoking an invitation (admin)', () => {
  it('asks first, then revokes and refreshes the list', async () => {
    const s = spy();
    renderPage();
    await table().findByText('sam@acme.com');
    await userEvent.click(screen.getByRole('button', { name: 'Revoke invitation for sam@acme.com' }));
    const confirm = await screen.findByRole('dialog');
    expect(within(confirm).getByText('Revoke invitation?')).toBeInTheDocument();
    expect(s.deletes).toHaveLength(0);
    await userEvent.click(within(confirm).getByRole('button', { name: 'Revoke' }));
    await waitFor(() => expect(s.deletes).toEqual(['inv_seed_1']));
    await waitFor(() => expect(table().queryByText('sam@acme.com')).not.toBeInTheDocument());
    expect(table().getByText('priya@globex.com')).toBeInTheDocument();
  });

  it('keeps the row and says so when the server refuses', async () => {
    server.use(
      http.delete('/v1/invitations/:id', () =>
        HttpResponse.json({ error: { message: 'This invitation was already accepted', type: 'conflict' } }, { status: 409 }),
      ),
    );
    renderPage();
    await table().findByText('sam@acme.com');
    await userEvent.click(screen.getByRole('button', { name: 'Revoke invitation for sam@acme.com' }));
    await userEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Revoke' }));
    expect(await screen.findByText('This invitation was already accepted')).toBeInTheDocument();
    expect(table().getByText('sam@acme.com')).toBeInTheDocument();
  });
});

describe('Invitations in the navigation (admin)', () => {
  it('is a regular sidebar link like the others, not a button', () => {
    renderWith(<Sidebar />);
    expect(screen.getByRole('link', { name: 'Invitations' })).toHaveAttribute('href', '/invitations');
    expect(screen.queryByRole('button', { name: /^Invite/ })).not.toBeInTheDocument();
  });

  it('renders at /invitations and marks the link active', async () => {
    renderWith(<AppRoutes />, '/invitations');
    expect(await screen.findByRole('heading', { name: 'Invitations' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Invitations' })).toHaveAttribute('aria-current', 'page');
  });
});

describe('Email delivery of a new invitation (admin)', () => {
  async function invite(status: 'sent' | 'failed' | 'not_configured' | 'rejected', detail: string | null = null) {
    server.use(
      http.post('/v1/invitations', async () =>
        HttpResponse.json(
          {
            id: 'inv_x', email: 'jane@example.com', name: 'Jane Doe', organization_id: GLOBEX.id,
            organization_name: GLOBEX.name, role_id: 'r', role_name: 'org_user',
            expires_at: new Date(Date.now() + 7 * 86400000).toISOString(),
            invite_path: status === 'sent' ? null : TOKEN_PATH, invite_url: null, organization_created: false,
            email_status: status, email_detail: detail,
          },
          { status: 201 },
        ),
      ),
    );
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: 'Invite member' }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText('Name'), 'Jane Doe');
    await userEvent.type(within(dialog).getByLabelText('Email'), 'jane@example.com');
    const orgs = within(dialog).getByLabelText('Select organization');
    await waitFor(() => expect(within(orgs).getAllByRole('option').length).toBeGreaterThan(1));
    await userEvent.selectOptions(orgs, GLOBEX.name);
    await userEvent.click(within(dialog).getByRole('button', { name: 'Invite' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  }

  it('just confirms when the invitation was emailed: no link card, nothing to copy', async () => {
    await invite('sent');
    expect(await screen.findByText('Invitation sent')).toBeInTheDocument();
    expect(screen.getByText('We emailed the invitation to jane@example.com.')).toBeInTheDocument();
    expect(screen.queryByText('Invite link created')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Invitation link')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Copy link' })).not.toBeInTheDocument();
  });

  it('warns, with the reason, when the email could not be sent', async () => {
    await invite('failed', 'ConnectionRefusedError: [Errno 111] Connection refused');
    expect(await screen.findByText('Invite link created')).toBeInTheDocument(); // the fallback: link stays on screen
    expect(screen.getByLabelText('Invitation link')).toBeInTheDocument();
    expect(screen.getByText('The email could not be sent')).toBeInTheDocument();
    expect(screen.getByText(/ConnectionRefusedError/)).toBeInTheDocument();
  });

  it('says the address was rejected, with the mail server\'s reason, and keeps the link', async () => {
    await invite('rejected', '550 5.1.1 The email account that you tried to reach does not exist.');
    expect(await screen.findByText('That email address was rejected')).toBeInTheDocument();
    expect(screen.getByText(/does not exist/)).toBeInTheDocument();
    expect(screen.getByLabelText('Invitation link')).toBeInTheDocument();
    expect(screen.queryByText('Invitation sent')).not.toBeInTheDocument();
  });

  it('tells the admin to send the link themselves when email is not set up', async () => {
    await invite('not_configured');
    expect(await screen.findByText('Invite link created')).toBeInTheDocument();
    expect(screen.getByLabelText('Invitation link')).toBeInTheDocument();
    expect(screen.getByText(/Email isn.t set up, so nothing was sent/)).toBeInTheDocument();
  });
});
