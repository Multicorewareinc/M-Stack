import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { useAuth } from './AuthContext';
import { MockAuthProvider } from './MockAuthProvider';

function Probe() {
  const { user, organizationContext, permissions } = useAuth();
  return (
    <div>
      <span data-testid="name">{user.displayName}</span>
      <span data-testid="org">{organizationContext.name}</span>
      <span data-testid="perm-count">{permissions.length}</span>
    </div>
  );
}

describe('useAuth (mock provider)', () => {
  it('exposes mock identity, org context, and permissions', () => {
    const queryClient = new QueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <MockAuthProvider>
          <Probe />
        </MockAuthProvider>
      </QueryClientProvider>,
    );
    expect(screen.getByTestId('name')).toHaveTextContent('Org Admin');
    expect(screen.getByTestId('org')).toHaveTextContent('Acme Corporation');
    expect(Number(screen.getByTestId('perm-count').textContent)).toBeGreaterThan(0);
  });
});
