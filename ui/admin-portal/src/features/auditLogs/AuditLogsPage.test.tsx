import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { server } from '../../mocks/server';
import { actionLabel } from './auditActions';
import { AuditLogsPage } from './AuditLogsPage';

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuditLogsPage />
    </QueryClientProvider>,
  );
}

// Event labels also appear as <option>s in the filter, so row assertions are scoped to the table.
const table = () => within(screen.getByRole('table'));

describe('AuditLogsPage', () => {
  it('lists audit events across organizations, resolving organization names', async () => {
    renderPage();
    expect(await within(await screen.findByRole('table')).findByText('ci-pipeline')).toBeInTheDocument();
    expect(table().getByText('API key revoked')).toBeInTheDocument();
    expect(table().getByText('User created')).toBeInTheDocument();
    expect(table().getByText('Organization created')).toBeInTheDocument();
    expect(table().getByText('john@acme.com')).toBeInTheDocument();
    expect((await table().findAllByText('Acme Corp')).length).toBeGreaterThan(0);
    expect(screen.getByText('Showing 1–3 of 3')).toBeInTheDocument();
  });

  it('sends the selected organization filter to the API and resets to the first page', async () => {
    const seen: URL[] = [];
    server.events.on('request:start', ({ request }) => {
      const url = new URL(request.url);
      if (url.pathname === '/v1/audit-logs') seen.push(url);
    });
    renderPage();
    await within(await screen.findByRole('table')).findByText('ci-pipeline');

    const select = await screen.findByLabelText('Filter by organization');
    await waitFor(() => expect(select.querySelectorAll('option').length).toBeGreaterThan(2));
    await userEvent.selectOptions(select, 'org_002');

    await waitFor(() => expect(table().queryByText('ci-pipeline')).not.toBeInTheDocument());
    expect(table().getByText('User created')).toBeInTheDocument();
    const last = seen[seen.length - 1];
    expect(last.searchParams.get('organization_id')).toBe('org_002');
    expect(last.searchParams.get('offset')).toBe('0');
  });

  it('sends a rolling time-range preset as `since` and returns to the first page', async () => {
    const seen: URL[] = [];
    server.events.on('request:start', ({ request }) => {
      const url = new URL(request.url);
      if (url.pathname === '/v1/audit-logs') seen.push(url);
    });
    renderPage();
    await within(await screen.findByRole('table')).findByText('ci-pipeline');
    expect(seen[0].searchParams.get('since')).toBeNull(); // "All time" by default

    const before = Date.now();
    await userEvent.selectOptions(screen.getByLabelText('Filter by time range'), '24h');
    await waitFor(() => expect(seen[seen.length - 1].searchParams.get('since')).not.toBeNull());
    const last = seen[seen.length - 1];
    const since = new Date(last.searchParams.get('since') as string).getTime();
    expect(since).toBeGreaterThanOrEqual(before - 24 * 3600 * 1000 - 1000);
    expect(since).toBeLessThanOrEqual(Date.now() - 24 * 3600 * 1000 + 1000);
    expect(last.searchParams.get('until')).toBeNull();
    expect(last.searchParams.get('offset')).toBe('0');
    // the fixtures are from June 2026, so nothing falls in the last 24 hours
    expect(await screen.findByText('No audit events')).toBeInTheDocument();
  });

  it('filters to a custom date range and rejects an inverted one', async () => {
    const seen: URL[] = [];
    server.events.on('request:start', ({ request }) => {
      const url = new URL(request.url);
      if (url.pathname === '/v1/audit-logs') seen.push(url);
    });
    renderPage();
    await within(await screen.findByRole('table')).findByText('ci-pipeline');
    expect(screen.queryByLabelText('From date')).not.toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText('Filter by time range'), 'custom');
    await userEvent.type(screen.getByLabelText('From date'), '2026-06-01');
    await userEvent.type(screen.getByLabelText('To date'), '2026-06-02');
    await waitFor(() => expect(table().queryByText('ci-pipeline')).not.toBeInTheDocument());
    expect(table().getByText('User created')).toBeInTheDocument();
    expect(table().getByText('Organization created')).toBeInTheDocument();
    const last = seen[seen.length - 1];
    expect(last.searchParams.get('since')).toBe(new Date('2026-06-01T00:00:00').toISOString());
    expect(last.searchParams.get('until')).toBe(new Date('2026-06-02T23:59:59.999').toISOString());

    await userEvent.clear(screen.getByLabelText('To date'));
    await userEvent.type(screen.getByLabelText('To date'), '2026-06-02');
    await userEvent.clear(screen.getByLabelText('From date'));
    await userEvent.type(screen.getByLabelText('From date'), '2026-06-05');
    expect(await screen.findByRole('alert')).toHaveTextContent(/must be on or before/);
    expect(seen[seen.length - 1].searchParams.get('since')).toBeNull(); // an invalid range applies no bounds
  });

  it('has no IP address column; the IP is shown in the event details instead', async () => {
    renderPage();
    const row = (await within(await screen.findByRole('table')).findByText('ci-pipeline')).closest('tr') as HTMLElement;
    expect(screen.queryByRole('columnheader', { name: 'IP address' })).not.toBeInTheDocument();
    expect(within(row).queryByText('10.0.0.4')).not.toBeInTheDocument();

    await userEvent.click(within(row).getByRole('button', { name: /View details for API key revoked/ }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('IP address')).toBeInTheDocument();
    expect(within(dialog).getByText('10.0.0.4')).toBeInTheDocument();
    expect(within(dialog).getByText('Organization member')).toBeInTheDocument();
    expect(await within(dialog).findByText('Acme Corporation')).toBeInTheDocument();
    expect(within(dialog).getByText('Organization control plane')).toBeInTheDocument();
    expect(within(dialog).getByText('No additional details were recorded for this event.')).toBeInTheDocument();

    await userEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('shows the recorded payload and a dash for a missing IP in the details', async () => {
    renderPage();
    const row = (await within(await screen.findByRole('table')).findByText('jane@globex.com')).closest('tr') as HTMLElement;
    await userEvent.click(within(row).getByRole('button', { name: /View details for User created/ }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/"username": "jane"/)).toBeInTheDocument();
    expect(within(dialog).getByText('Platform admin')).toBeInTheDocument();
    expect(within(dialog).getByText('IP address').nextElementSibling).toHaveTextContent('—');
  });

  it('paginates with Previous/Next', async () => {
    const page = (offset: number) => ({
      items: [
        {
          id: `a${offset}`,
          organization_id: null,
          actor_type: 'admin',
          actor_id: null,
          actor_email: 'root@platform.local',
          action: 'plan.created',
          resource_type: 'plan',
          resource_id: null,
          resource_name: `Plan ${offset}`,
          details: {},
          ip_address: null,
          created_at: '2026-06-01T08:00:00Z',
          source: 'platform',
        },
      ],
      total: 60,
      limit: 25,
      offset,
    });
    server.use(
      http.get('/v1/audit-logs', ({ request }) =>
        HttpResponse.json(page(Number(new URL(request.url).searchParams.get('offset') ?? 0))),
      ),
    );
    renderPage();
    expect(await screen.findByText('Plan 0')).toBeInTheDocument();
    expect(screen.getByText('Showing 1–25 of 60')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled();

    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(await screen.findByText('Plan 25')).toBeInTheDocument();
    expect(screen.getByText('Showing 26–50 of 60')).toBeInTheDocument();
  });

  it('shows an empty state and an error state', async () => {
    server.use(http.get('/v1/audit-logs', () => HttpResponse.json({ items: [], total: 0, limit: 25, offset: 0 })));
    const { unmount } = renderPage();
    expect(await screen.findByText('No audit events')).toBeInTheDocument();
    unmount();

    server.use(
      http.get('/v1/audit-logs', () =>
        HttpResponse.json({ error: { message: 'Org CP unavailable', type: 'upstream_error' } }, { status: 502 }),
      ),
    );
    renderPage();
    expect(await screen.findByText('Org CP unavailable')).toBeInTheDocument();
  });
});

describe('actionLabel', () => {
  it('humanizes dotted event names', () => {
    expect(actionLabel('api_key.created')).toBe('API key created');
    expect(actionLabel('role.permissions_updated')).toBe('Role permissions updated');
    expect(actionLabel('auth.login_failed')).toBe('Auth login failed');
  });
});
