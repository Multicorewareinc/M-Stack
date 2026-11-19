import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { MockAuthProvider } from '../auth/MockAuthProvider';
import { RequirePermission } from './RequirePermission';

function renderWithAuth(ui: React.ReactNode, permissions: string[]) {
  const queryClient = new QueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <MockAuthProvider permissions={permissions}>{ui}</MockAuthProvider>
    </QueryClientProvider>,
  );
}

describe('RequirePermission', () => {
  it('renders children when the permission is present', () => {
    renderWithAuth(
      <RequirePermission permission="roles.read">
        <p>Roles page content</p>
      </RequirePermission>,
      ['roles.read'],
    );
    expect(screen.getByText('Roles page content')).toBeInTheDocument();
    expect(screen.queryByText('Access denied')).toBeNull();
  });

  it('shows Access Denied instead of children when the permission is missing', () => {
    renderWithAuth(
      <RequirePermission permission="roles.read">
        <p>Roles page content</p>
      </RequirePermission>,
      ['users.read'],
    );
    // Must fail if the gated page renders anyway for a user without the permission.
    expect(screen.queryByText('Roles page content')).toBeNull();
    expect(screen.getByText('Access denied')).toBeInTheDocument();
  });
});
