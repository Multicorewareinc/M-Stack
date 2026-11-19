import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MockAuthProvider } from '../../auth/MockAuthProvider';
import { server } from '../../mocks/server';
import { actionLabel, actorLabel } from './auditActions';
import { AuditLogsPage } from './AuditLogsPage';

const log = (over: Record<string, unknown> = {}) => ({
  id: crypto.randomUUID(),
  organization_id: 'org_001',
  actor_type: 'user',
  actor_id: 'usr_001',
  actor_email: 'john@acme.com',
  action: 'api_key.created',
  resource_type: 'api_key',
  resource_id: 'key_001',
  resource_name: 'ci-pipeline',
  details: {},
  ip_address: '10.0.0.4',
  created_at: '2026-06-03T09:30:00Z',
  ...over,
});

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MockAuthProvider>
        <AuditLogsPage />
      </MockAuthProvider>
    </QueryClientProvider>,
  );
}

// Event labels also appear as <option>s in the filter, so row assertions are scoped to the table.
const table = () => within(screen.getByRole('table'));

describe('AuditLogsPage', () => {
  beforeEach(() => vi.stubEnv('VITE_REAL_AUTH', 'true'));
  afterEach(() => vi.unstubAllEnvs());

  it('lists this organization’s events from /api/audit-logs', async () => {
    server.use(
      http.get('/api/audit-logs', () =>
        HttpResponse.json({
          items: [
            log(),
            log({ actor_type: 'admin', actor_email: 'root@platform.local', action: 'user.created', resource_name: 'jane@acme.com' }),
          ],
          total: 2,
          limit: 25,
          offset: 0,
        }),
      ),
    );
    renderPage();
    expect(await within(await screen.findByRole('table')).findByText('ci-pipeline')).toBeInTheDocument();
    expect(table().getByText('API key created')).toBeInTheDocument();
    expect(table().getByText('john@acme.com')).toBeInTheDocument();
    expect(table().getByText('root@platform.local (platform admin)')).toBeInTheDocument();
    expect(screen.getByText('Showing 1–2 of 2')).toBeInTheDocument();
    // No organization column — the whole page is already one organization.
    expect(screen.queryByRole('columnheader', { name: 'Organization' })).not.toBeInTheDocument();
  });

  it('sends the event filter and paginates', async () => {
    const seen: URL[] = [];
    server.use(
      http.get('/api/audit-logs', ({ request }) => {
        const url = new URL(request.url);
        seen.push(url);
        const offset = Number(url.searchParams.get('offset'));
        return HttpResponse.json({
          items: [log({ resource_name: `key-${offset}` })],
          total: 60,
          limit: 25,
          offset,
        });
      }),
    );
    renderPage();
    expect(await within(await screen.findByRole('table')).findByText('key-0')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled();

    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(await table().findByText('key-25')).toBeInTheDocument();
    expect(screen.getByText('Showing 26–50 of 60')).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText('Filter by event'), 'api_key.revoked');
    await waitFor(() => expect(seen[seen.length - 1].searchParams.get('action')).toBe('api_key.revoked'));
    expect(seen[seen.length - 1].searchParams.get('offset')).toBe('0'); // a new filter returns to page 1
  });

  it('sends the time range as since/until and returns to the first page', async () => {
    const seen: URL[] = [];
    server.use(
      http.get('/api/audit-logs', ({ request }) => {
        seen.push(new URL(request.url));
        return HttpResponse.json({ items: [log()], total: 1, limit: 25, offset: 0 });
      }),
    );
    renderPage();
    await within(await screen.findByRole('table')).findByText('ci-pipeline');
    expect(seen[0].searchParams.get('since')).toBeNull(); // "All time" by default

    await userEvent.selectOptions(screen.getByLabelText('Filter by time range'), '7d');
    await waitFor(() => expect(seen[seen.length - 1].searchParams.get('since')).not.toBeNull());
    expect(seen[seen.length - 1].searchParams.get('until')).toBeNull();
    expect(seen[seen.length - 1].searchParams.get('offset')).toBe('0');

    await userEvent.selectOptions(screen.getByLabelText('Filter by time range'), 'custom');
    await userEvent.type(screen.getByLabelText('From date'), '2026-06-01');
    await userEvent.type(screen.getByLabelText('To date'), '2026-06-02');
    await waitFor(() =>
      expect(seen[seen.length - 1].searchParams.get('until')).toBe(new Date('2026-06-02T23:59:59.999').toISOString()),
    );
    expect(seen[seen.length - 1].searchParams.get('since')).toBe(new Date('2026-06-01T00:00:00').toISOString());

    await userEvent.clear(screen.getByLabelText('From date'));
    await userEvent.type(screen.getByLabelText('From date'), '2026-06-05');
    expect(await screen.findByRole('alert')).toHaveTextContent(/must be on or before/);
    await waitFor(() => expect(seen[seen.length - 1].searchParams.get('since')).toBeNull());
  });

  it('has no IP address column; the IP and payload are shown in the event details', async () => {
    server.use(
      http.get('/api/audit-logs', () =>
        HttpResponse.json({
          items: [log({ details: { name: 'ci-pipeline', scopes: ['read'] } })],
          total: 1,
          limit: 25,
          offset: 0,
        }),
      ),
    );
    renderPage();
    const row = (await within(await screen.findByRole('table')).findByText('ci-pipeline')).closest('tr') as HTMLElement;
    expect(screen.queryByRole('columnheader', { name: 'IP address' })).not.toBeInTheDocument();
    expect(within(row).queryByText('10.0.0.4')).not.toBeInTheDocument();

    await userEvent.click(within(row).getByRole('button', { name: /View details for API key created/ }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('10.0.0.4')).toBeInTheDocument();
    expect(within(dialog).getByText('Organization member')).toBeInTheDocument();
    expect(within(dialog).getByText('usr_001')).toBeInTheDocument();
    expect(within(dialog).getByText(/"scopes"/)).toBeInTheDocument();

    await userEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('shows the API error message', async () => {
    server.use(
      http.get('/api/audit-logs', () =>
        HttpResponse.json({ error: { message: 'Forbidden', type: 'forbidden' } }, { status: 403 }),
      ),
    );
    renderPage();
    expect(await screen.findByText('Forbidden')).toBeInTheDocument();
  });

  it('needs a real signed-in session', () => {
    vi.stubEnv('VITE_REAL_AUTH', 'false');
    renderPage();
    expect(screen.getByText('Requires a real signed-in session')).toBeInTheDocument();
  });
});

describe('audit labels', () => {
  it('humanizes events and attributes service/admin actors', () => {
    expect(actionLabel('api_key.revoked')).toBe('API key revoked');
    expect(actorLabel({ actor_type: 'service', actor_email: null })).toBe('System');
    expect(actorLabel({ actor_type: 'admin', actor_email: 'a@b.io' })).toBe('a@b.io (platform admin)');
  });
});
