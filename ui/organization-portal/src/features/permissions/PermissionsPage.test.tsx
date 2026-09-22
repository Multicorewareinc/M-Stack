import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it, vi } from 'vitest';
import { MockAuthProvider } from '../../auth/MockAuthProvider';
import { server } from '../../mocks/server';
import { PermissionsPage } from './PermissionsPage';

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MockAuthProvider>
        <PermissionsPage />
      </MockAuthProvider>
    </QueryClientProvider>,
  );
}

describe('PermissionsPage', () => {
  it('loads the catalog with a single request', async () => {
    const requestSpy = vi.fn();
    server.events.on('request:start', ({ request }) => {
      if (new URL(request.url).pathname === '/v1/permissions') requestSpy();
    });
    renderPage();
    // Must fail if a second/duplicate request is issued.
    await screen.findByText('users.read');
    expect(requestSpy).toHaveBeenCalledTimes(1);
  });

  it('search filters the catalog', async () => {
    renderPage();
    await screen.findByText('users.read');
    expect(screen.getByText('roles.read')).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText('Search permissions'), 'users.read');

    // Must fail if search does not affect the displayed permissions.
    await waitFor(() => expect(screen.queryByText('roles.read')).toBeNull());
    expect(screen.getByText('users.read')).toBeInTheDocument();
  });

  it('has no create, edit, or delete controls', async () => {
    renderPage();
    await screen.findByText('users.read');
    // Must fail if any such control is present.
    expect(screen.queryByRole('button', { name: /create/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /edit/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /delete/i })).toBeNull();
  });

  it('shows a read-only indicator', async () => {
    renderPage();
    // Must fail if the indicator is absent.
    expect(await screen.findByText('Read-only')).toBeInTheDocument();
  });

  it('shows an empty state when search matches nothing', async () => {
    renderPage();
    await screen.findByText('users.read');

    await userEvent.type(screen.getByLabelText('Search permissions'), 'no-such-permission');

    // Must fail if the table silently renders zero rows with no explanatory state.
    expect(await screen.findByText('No permissions found')).toBeInTheDocument();
  });

  it('shows an inline error when the catalog request fails', async () => {
    server.use(http.get('/v1/permissions', () => HttpResponse.json({ error: { message: 'Internal error', type: 'internal_error' } }, { status: 500 })));
    renderPage();
    // Must fail if the page crashes or silently renders an empty table.
    expect(await screen.findByText(/internal error/i)).toBeInTheDocument();
  });
});
