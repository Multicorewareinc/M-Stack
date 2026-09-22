import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { server } from '../../mocks/server';
import { UsersDirectoryPage } from './UsersDirectoryPage';

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <UsersDirectoryPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('UsersDirectoryPage', () => {
  it('loads the directory with a single request', async () => {
    const urls: string[] = [];
    const listener = ({ request }: { request: Request }) => {
      if (new URL(request.url).pathname === '/v1/users') urls.push(request.url);
    };
    server.events.on('request:start', listener);

    renderPage();
    await screen.findByText('John Smith');

    // Must fail if a per-row detail request is issued.
    expect(urls.length).toBe(1);
    server.events.removeListener('request:start', listener);
  });

  it('search filters the directory', async () => {
    renderPage();
    await screen.findByText('John Smith');
    expect(screen.getByText('Jane Doe')).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText('Search users'), 'John');

    // Must fail if typing does not affect the displayed rows.
    await waitFor(() => expect(screen.queryByText('Jane Doe')).toBeNull());
    expect(screen.getByText('John Smith')).toBeInTheDocument();
  });

  it('offers no create/edit/delete affordances', async () => {
    renderPage();
    await screen.findByText('John Smith');
    // Must fail if any mutation control is present.
    expect(screen.queryByRole('button', { name: /create/i })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Row actions' })).toBeNull();
  });
});
