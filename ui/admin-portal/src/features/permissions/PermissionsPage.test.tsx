import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { MockAuthProvider } from '../../auth/MockAuthProvider';
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
  it('labels the page as the platform master catalog', async () => {
    renderPage();
    // Must fail if the heading or ownership copy is missing.
    expect(await screen.findByRole('heading', { name: 'Master Permission Catalog' })).toBeInTheDocument();
    expect(screen.getByText(/only super admins can modify the master permission list/i)).toBeInTheDocument();
  });

  it('renders seeded permissions', async () => {
    renderPage();
    expect(await screen.findByText('users.read')).toBeInTheDocument();
    expect(screen.getByText('users.create')).toBeInTheDocument();
    // Must fail if a column is missing or wrong.
    expect(screen.getAllByText('users').length).toBeGreaterThan(0);
    expect(screen.getByText('read')).toBeInTheDocument();
  });

  it('search filters by slug', async () => {
    renderPage();
    await screen.findByText('users.read');
    expect(screen.getByText('users.create')).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText('Search permissions'), 'users.read');

    // Must fail if search does not affect the rows.
    await waitFor(() => expect(screen.queryByText('users.create')).toBeNull());
    expect(screen.getByText('users.read')).toBeInTheDocument();
  });
});
