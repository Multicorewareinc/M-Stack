import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { MockAuthProvider } from '../../auth/MockAuthProvider';
import { RoleDetailPage } from './RoleDetailPage';

function renderDetail(roleId: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MockAuthProvider>
        <MemoryRouter initialEntries={[`/roles/${roleId}`]}>
          <Routes>
            <Route path="/roles/:roleId" element={<RoleDetailPage />} />
          </Routes>
        </MemoryRouter>
      </MockAuthProvider>
    </QueryClientProvider>,
  );
}

describe('RoleDetailPage', () => {
  it('renders Permissions and Users tabs, with no Overview tab', async () => {
    renderDetail('role_admin');
    expect(await screen.findByRole('heading', { name: 'Admin' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Permissions' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Users' })).toBeInTheDocument();
    // Must fail if Overview is reintroduced — its Users/Permissions stat cards were replaced by
    // the count now shown directly on the Users tab.
    expect(screen.queryByRole('tab', { name: 'Overview' })).toBeNull();
  });

  it('shows the count of users assigned to this role under the Users tab', async () => {
    renderDetail('role_admin');
    await screen.findByRole('heading', { name: 'Admin' });
    await userEvent.click(screen.getByRole('tab', { name: 'Users' }));

    // Must fail if the count is missing or wrong — the fixtures assign exactly one user
    // (usr_001) to role_admin.
    expect(await screen.findByText('Users assigned to this role')).toBeInTheDocument();
    expect(screen.getByText('1')).toBeInTheDocument();
  });

  it('renders not-found for a missing role', async () => {
    renderDetail('role_does_not_exist');
    // Must fail if the page crashes or renders nothing.
    expect(await screen.findByText('Role not found')).toBeInTheDocument();
  });
});
