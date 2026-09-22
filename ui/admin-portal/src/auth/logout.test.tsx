import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { useAuth } from './AuthContext';
import { MockAuthProvider } from './MockAuthProvider';

function Probe() {
  const { logout } = useAuth();
  const { data } = useQuery({ queryKey: ['probe'], queryFn: () => Promise.resolve('cached-value') });
  return (
    <div>
      <span data-testid="data">{data ?? 'none'}</span>
      <button onClick={logout}>Logout</button>
    </div>
  );
}

describe('logout', () => {
  it('clears cached server and local state', async () => {
    const queryClient = new QueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <MockAuthProvider>
          <Probe />
        </MockAuthProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('data')).toHaveTextContent('cached-value'));
    expect(queryClient.getQueryData(['probe'])).toBe('cached-value');

    await userEvent.click(screen.getByRole('button', { name: 'Logout' }));
    // Must fail if a query cache entry survives logout.
    expect(queryClient.getQueryData(['probe'])).toBeUndefined();
  });
});
