import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { MockAuthProvider } from '../../auth/MockAuthProvider';
import { Sidebar } from './Sidebar';

function renderSidebar(permissions: string[]) {
  const queryClient = new QueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <MockAuthProvider permissions={permissions}>
        <MemoryRouter>
          <Sidebar />
        </MemoryRouter>
      </MockAuthProvider>
    </QueryClientProvider>,
  );
}

describe('Sidebar', () => {
  it('shows every gated nav item when all their permissions are held', () => {
    renderSidebar(['users.read', 'roles.read', 'apikey.create']);
    for (const label of ['Overview', 'Users', 'Roles', 'Permissions', 'API Keys', 'Chat', 'Billing & Usage']) {
      expect(screen.getByRole('link', { name: label })).toBeInTheDocument();
    }
  });

  it('hides Users, Roles, and Permissions without roles.read/users.read', () => {
    renderSidebar([]);
    // Must fail if a gated link still renders for a user with no matching permission.
    expect(screen.queryByRole('link', { name: 'Users' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'Roles' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'Permissions' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'API Keys' })).toBeNull();
    // Must fail if an ungated item is accidentally hidden too.
    expect(screen.getByRole('link', { name: 'Overview' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Chat' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Billing & Usage' })).toBeInTheDocument();
  });

  it('shows Permissions and Roles for roles.read alone, independent of users.read', () => {
    renderSidebar(['roles.read']);
    expect(screen.getByRole('link', { name: 'Roles' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Permissions' })).toBeInTheDocument();
    // Must fail if roles.read incorrectly also unlocks Users.
    expect(screen.queryByRole('link', { name: 'Users' })).toBeNull();
  });

  it.each(['apikey.create', 'apikey.update', 'apikey.delete'])(
    'shows API Keys when the user holds only %s (OR match across the granular actions)',
    (slug) => {
      renderSidebar([slug]);
      expect(screen.getByRole('link', { name: 'API Keys' })).toBeInTheDocument();
    },
  );
});
