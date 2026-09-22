import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
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
  it('renders Overview, Permissions, Users tabs', async () => {
    renderDetail('role_admin');
    expect(await screen.findByRole('heading', { name: 'Admin' })).toBeInTheDocument();
    // Must fail if a tab is missing.
    expect(screen.getByRole('tab', { name: 'Overview' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Permissions' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Users' })).toBeInTheDocument();
  });

  it('renders not-found for a missing role', async () => {
    renderDetail('role_does_not_exist');
    // Must fail if the page crashes or renders nothing.
    expect(await screen.findByText('Role not found')).toBeInTheDocument();
  });
});
