import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { MockAuthProvider } from '../auth/MockAuthProvider';
import { Can } from './Can';

function renderWithAuth(ui: React.ReactNode, permissions: string[]) {
  const queryClient = new QueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <MockAuthProvider permissions={permissions}>{ui}</MockAuthProvider>
    </QueryClientProvider>,
  );
}

describe('Can', () => {
  it('hides children without permission', () => {
    renderWithAuth(
      <Can permission="users.create">
        <button>Create User</button>
      </Can>,
      ['organizations.read'],
    );
    // Must fail if content renders regardless of permission.
    expect(screen.queryByRole('button', { name: 'Create User' })).toBeNull();
  });

  it('renders children with permission', () => {
    renderWithAuth(
      <Can permission="organizations.read">
        <span>Organizations</span>
      </Can>,
      ['organizations.read'],
    );
    expect(screen.getByText('Organizations')).toBeInTheDocument();
  });
});
