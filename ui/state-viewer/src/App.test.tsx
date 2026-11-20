import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { RefreshProvider } from './app/RefreshContext';
import { Shell } from './app/Shell';
import { ActivityPage } from './features/activity/ActivityPage';
import { DeploymentsPage } from './features/deployments/DeploymentsPage';
import { Route, Routes } from 'react-router-dom';
import { deployments, DB, installFakeApi, type FakeOptions } from './test/fakeApi';
import { deploymentMeta, formatDateTime, timeAgo } from './lib/format';

function renderApp(path = '/', options: FakeOptions = {}) {
  const requests = installFakeApi(options);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <RefreshProvider initial="off">
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route element={<Shell />}>
              <Route path="/" element={<DeploymentsPage />} />
              <Route path="/activity" element={<ActivityPage />} />
            </Route>
          </Routes>
        </MemoryRouter>
      </RefreshProvider>
    </QueryClientProvider>,
  );
  return requests;
}

// Status labels also appear as <option>s in the filters, so row assertions are scoped to the table.
const table = () => within(screen.getByRole('table'));
const waitForRows = async () => within(await screen.findByRole('table')).findByText('edge-cluster');

describe('Deployments', () => {
  it('lists every deployment with its layer, status, latest result and error', async () => {
    renderApp();
    await waitForRows();

    expect(table().getByText('longhorn')).toBeInTheDocument();
    expect(table().getByText('default/chat-route')).toBeInTheDocument();
    expect(table().getByText('Provisioned')).toBeInTheDocument();
    expect(table().getAllByText('Failed').length).toBeGreaterThan(0);
    expect(table().getByText('helm timed out after 600s', { selector: 'span.line-clamp-1' })).toBeInTheDocument();
    expect(table().getByText('storage')).toBeInTheDocument();
    expect(screen.queryByText(DB)).not.toBeInTheDocument(); // the file path is not shown in the header
  });

  it('shows summary counts', async () => {
    renderApp();
    await waitForRows();
    const total = screen.getByRole('button', { name: /Total/ });
    expect(total).toHaveTextContent('3');
    expect(screen.getByRole('button', { name: /Failed/ })).toHaveTextContent('1');
    expect(screen.getByRole('button', { name: /Degraded/ })).toHaveTextContent('1');
  });

  it('filters by clicking a status card, and clears it again', async () => {
    const requests = renderApp();
    await waitForRows();

    await userEvent.click(screen.getByRole('button', { name: /Failed/ }));
    await waitFor(() => expect(table().queryByText('edge-cluster')).not.toBeInTheDocument());
    expect(table().getByText('longhorn')).toBeInTheDocument();
    expect(requests.some((u) => u.pathname === '/api/deployments' && u.searchParams.get('status') === 'failed')).toBe(true);

    await userEvent.click(screen.getByRole('button', { name: /Total/ }));
    expect(await table().findByText('edge-cluster')).toBeInTheDocument();
  });

  it('filters by layer and by search text', async () => {
    renderApp();
    await waitForRows();

    await userEvent.selectOptions(screen.getByLabelText('Filter by layer'), 'cluster');
    await waitFor(() => expect(table().queryByText('longhorn')).not.toBeInTheDocument());
    await userEvent.selectOptions(screen.getByLabelText('Filter by layer'), 'all');

    await userEvent.type(screen.getByLabelText('Search deployments'), 'timed out');
    await waitFor(() => expect(table().queryByText('edge-cluster')).not.toBeInTheDocument());
    expect(table().getByText('longhorn')).toBeInTheDocument();
  });

  it('opens a deployment’s detail with its error and status history, and the URL carries it', async () => {
    renderApp();
    await waitForRows();

    await userEvent.click(table().getByText('longhorn'));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('heading', { name: 'longhorn' })).toBeInTheDocument();
    expect(await within(dialog).findByText('Unhealthy')).toBeInTheDocument(); // the history row
    expect(within(dialog).getAllByText('helm timed out after 600s').length).toBeGreaterThan(0);
  });

  it('can open a deployment whose name contains a slash', async () => {
    renderApp();
    await waitForRows();
    await userEvent.click(table().getByText('default/chat-route'));
    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByRole('heading', { name: 'default/chat-route' })).toBeInTheDocument();
    expect(within(dialog).getByText('backend slow')).toBeInTheDocument();
  });

  it('opens straight to a detail from a link (?d=)', async () => {
    renderApp('/?d=edge-cluster');
    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByRole('heading', { name: 'edge-cluster' })).toBeInTheDocument();
    expect(within(dialog).getByText('/tmp/kubeconfig')).toBeInTheDocument();
  });

  it('says so when a linked deployment no longer exists', async () => {
    renderApp('/?d=gone');
    expect(await screen.findByText(/Cannot show “gone”/)).toBeInTheDocument();
  });

  it('has no write controls — the viewer is read-only', async () => {
    renderApp();
    await waitForRows();
    expect(screen.queryByRole('button', { name: /delete|remove|create|retry|apply/i })).not.toBeInTheDocument();
  });
});

describe('A new attempt after a failure', () => {
  it('does not present the previous attempt’s result as the current one', async () => {
    const retrying = {
      ...deployments[1], // longhorn: failed earlier, now being re-deployed
      status: 'provisioning' as const,
      error: null,
    };
    renderApp('/', { rows: [deployments[0], retrying] });
    await waitForRows();

    const row = table().getByText('longhorn').closest('tr') as HTMLElement;
    expect(within(row).getByText('Provisioning')).toBeInTheDocument();
    expect(within(row).getByText('was Unhealthy')).toBeInTheDocument(); // muted, not a red "Unhealthy" badge
    expect(within(row).queryByText('Unhealthy', { selector: 'span.rounded-full' })).not.toBeInTheDocument();

    // a finished deployment still shows its result as a badge
    const done = table().getByText('edge-cluster').closest('tr') as HTMLElement;
    expect(within(done).getByText('Healthy')).toBeInTheDocument();
  });
});

describe('Refresh and relative times', () => {
  it('Refresh re-fetches the summary and the table', async () => {
    const requests = renderApp();
    await waitForRows();
    const count = (path: string) => requests.filter((u) => u.pathname === path).length;
    const before = { info: count('/api/info'), summary: count('/api/summary'), rows: count('/api/deployments') };

    await userEvent.click(screen.getByRole('button', { name: 'Refresh' }));

    await waitFor(() => {
      expect(count('/api/info')).toBeGreaterThan(before.info);
      expect(count('/api/summary')).toBeGreaterThan(before.summary);
      expect(count('/api/deployments')).toBeGreaterThan(before.rows);
    });
  });

  it('"x min ago" keeps moving with the clock while the data is unchanged', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] }); // react-query/DOM keep real timeouts
    vi.setSystemTime(new Date('2026-10-08T05:31:00Z'));
    try {
      renderApp(); // the fake returns identical rows every time, so only the clock can change the labels
      await waitForRows();
      expect(table().getAllByText('8 min ago').length).toBeGreaterThan(0); // default/chat-route, 05:23

      await act(async () => {
        vi.setSystemTime(new Date('2026-10-08T05:41:00Z')); // ten minutes pass
        vi.advanceTimersByTime(15_000); // one tick of the viewer's clock
      });

      expect(table().getAllByText('18 min ago').length).toBeGreaterThan(0);
      expect(table().queryByText('8 min ago')).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('Activity', () => {
  it('lists recent outcomes newest first', async () => {
    renderApp('/activity');
    const t = within(await screen.findByRole('table'));
    await t.findByText('edge-cluster'); // wait for real data, not the loading placeholder rows
    const rows = t.getAllByRole('row');
    expect(rows[1]).toHaveTextContent('default/chat-route'); // newest first (rows[0] is the header)
    expect(rows[3]).toHaveTextContent('edge-cluster');
    expect(screen.queryByText(/not each step along the way/)).not.toBeInTheDocument(); // no explanatory banner
  });

  it('filters by result and opens a deployment from a row', async () => {
    renderApp('/activity');
    await screen.findByRole('table');
    await userEvent.selectOptions(screen.getByLabelText('Filter by result'), 'unhealthy');
    await waitFor(() => expect(table().queryByText('edge-cluster')).not.toBeInTheDocument());

    await userEvent.click(table().getByText('longhorn'));
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
  });
});

describe('first-run and failure states', () => {
  it('explains a missing database instead of showing an empty table', async () => {
    renderApp('/', { exists: false, rows: [] });
    expect(await screen.findByText('No state database yet')).toBeInTheDocument();
    expect(screen.getByText(/Nothing has been deployed with the SDK/)).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('shows how to start the API when it cannot be reached', async () => {
    renderApp('/', { down: true });
    expect(await screen.findByText('Cannot read the state database')).toBeInTheDocument();
    expect(screen.getByText(/python -m multistack.state.viewer/)).toBeInTheDocument();
  });

  it('shows an empty-filter message when nothing matches', async () => {
    renderApp();
    await waitForRows();
    await userEvent.type(screen.getByLabelText('Search deployments'), 'zzz-nothing');
    expect(await screen.findByText('No deployments match')).toBeInTheDocument();
  });
});

describe('formatting', () => {
  it('labels known statuses and tolerates one the SDK adds later', () => {
    expect(deploymentMeta('provisioned')).toEqual({ label: 'Provisioned', variant: 'success' });
    expect(deploymentMeta('failed').variant).toBe('danger');
    expect(deploymentMeta('rolling_back')).toEqual({ label: 'Rolling_back', variant: 'neutral' });
  });

  it('formats times, and handles missing or bad values', () => {
    expect(formatDateTime(null)).toBe('—');
    expect(formatDateTime('not a date')).toBe('—');
    expect(formatDateTime('2026-10-08T05:00:09+00:00')).toMatch(/^\d{2} Oct 2026, \d{2}:\d{2}:09$/);
    const now = Date.parse('2026-10-08T05:30:00Z');
    expect(timeAgo('2026-10-08T05:29:58Z', now)).toBe('just now');
    expect(timeAgo('2026-10-08T05:25:00Z', now)).toBe('5 min ago');
    expect(timeAgo('2026-10-08T02:30:00Z', now)).toBe('3 h ago');
    expect(timeAgo('2026-10-06T05:30:00Z', now)).toBe('2 d ago');
    expect(timeAgo(undefined, now)).toBe('—');
    expect(deployments.length).toBe(3);
  });
});
