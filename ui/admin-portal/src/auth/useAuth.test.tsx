import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { useAuth } from './AuthContext';
import { MockAuthProvider } from './MockAuthProvider';

function Probe() {
  const { user, permissions, getAccessToken } = useAuth();
  return (
    <div>
      <span data-testid="name">{user.displayName}</span>
      <span data-testid="perm-count">{permissions.length}</span>
      <span data-testid="token">{String(getAccessToken())}</span>
    </div>
  );
}

describe('useAuth (mock provider)', () => {
  it('exposes mock identity and permissions', () => {
    const queryClient = new QueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <MockAuthProvider permissions={['organizations.read']}>
          <Probe />
        </MockAuthProvider>
      </QueryClientProvider>,
    );
    // Must fail if any field is undefined.
    expect(screen.getByTestId('name')).toHaveTextContent('Super Admin');
    expect(screen.getByTestId('perm-count')).toHaveTextContent('1');
    expect(screen.getByTestId('token')).toHaveTextContent('null');
  });
});
