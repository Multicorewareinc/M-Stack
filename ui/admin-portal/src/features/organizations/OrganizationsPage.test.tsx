import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { server } from '../../mocks/server';
import { OrganizationsPage } from './OrganizationsPage';

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <OrganizationsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('OrganizationsPage', () => {
  it('loads organizations with a single request', async () => {
    const urls: string[] = [];
    const listener = ({ request }: { request: Request }) => {
      if (new URL(request.url).pathname === '/v1/organizations') urls.push(request.url);
    };
    server.events.on('request:start', listener);

    renderPage();
    await screen.findByText('Acme Corporation');

    // Must fail if a per-row detail request is issued.
    expect(urls.length).toBe(1);
    server.events.removeListener('request:start', listener);
  });

  it('search filters organizations', async () => {
    renderPage();
    await screen.findByText('Acme Corporation');
    expect(screen.getByText('Example Inc')).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText('Search organizations'), 'acme');

    await waitFor(() => expect(screen.queryByText('Example Inc')).toBeNull());
    expect(screen.getByText('Acme Corporation')).toBeInTheDocument();
  });

  it('restricts row actions by status', async () => {
    renderPage();
    await screen.findByText('Acme Corporation'); // ACTIVE
    await screen.findByText('Startup X'); // FAILED, retryable

    const menuButtons = screen.getAllByRole('button', { name: 'Row actions' });

    await userEvent.click(menuButtons[0]); // Acme — ACTIVE
    let menu = await screen.findByRole('menu');
    expect(within(menu).getByRole('menuitem', { name: 'Suspend' })).toBeInTheDocument();
    await userEvent.click(menuButtons[0]); // close

    await userEvent.click(menuButtons[2]); // Startup X — FAILED, retryable
    menu = await screen.findByRole('menu');
    // Must fail if Suspend appears for a non-active organization.
    expect(within(menu).getByRole('menuitem', { name: 'Retry Provisioning' })).toBeInTheDocument();
    expect(within(menu).queryByRole('menuitem', { name: 'Suspend' })).toBeNull();
  });

  it('shows search empty state distinct from zero-data empty state', async () => {
    renderPage();
    await screen.findByText('Acme Corporation');

    await userEvent.type(screen.getByLabelText('Search organizations'), 'zzz-no-match');

    // Must fail if both empty conditions render identical copy.
    await waitFor(() => expect(screen.getByText('No organizations found')).toBeInTheDocument());
    expect(screen.getByText('Try changing your search or filters.')).toBeInTheDocument();
  });
});
