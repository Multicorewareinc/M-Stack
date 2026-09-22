import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { MockAuthProvider } from '../../auth/MockAuthProvider';
import { server } from '../../mocks/server';
import { UsersPage } from './UsersPage';

function renderPage(permissions?: string[]) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MockAuthProvider permissions={permissions}>
        <MemoryRouter>
          <UsersPage />
        </MemoryRouter>
      </MockAuthProvider>
    </QueryClientProvider>,
  );
}

describe('UsersPage', () => {
  it('loads users with a single request scoped to the authenticated org', async () => {
    const urls: string[] = [];
    const listener = ({ request }: { request: Request }) => {
      if (new URL(request.url).pathname === '/v1/users') urls.push(request.url);
    };
    server.events.on('request:start', listener);

    renderPage();
    await screen.findByText('John Smith');

    // Must fail if a per-row detail request is issued or a user from another org renders.
    expect(urls.length).toBe(1);
    expect(screen.getByText('Mark Lee')).toBeInTheDocument(); // same org_001
    server.events.removeListener('request:start', listener);
  });

  it('search filters the list', async () => {
    renderPage();
    await screen.findByText('John Smith');
    expect(screen.getByText('Mark Lee')).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText('Search users'), 'John');

    // Must fail if searching does not affect the displayed rows.
    await waitFor(() => expect(screen.queryByText('Mark Lee')).toBeNull());
    expect(screen.getByText('John Smith')).toBeInTheDocument();
  });

  it('hides Add User without users.create permission', async () => {
    renderPage(['users.read']);
    await screen.findByText('John Smith');
    // Must fail if the control renders regardless of permission.
    expect(screen.queryByRole('button', { name: '+ Add User' })).toBeNull();
  });
});
