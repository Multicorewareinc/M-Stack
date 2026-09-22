import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { queryKeys } from '../api/queryKeys';
import { MockAuthProvider } from './MockAuthProvider';

function Probe({ orgId }: { orgId: string }) {
  const { data } = useQuery({
    queryKey: queryKeys.users(orgId),
    queryFn: () => Promise.resolve(`users-of-${orgId}`),
  });
  return <span data-testid="data">{data ?? 'loading'}</span>;
}

describe('tenant-aware cache eviction', () => {
  it('evicts tenant-sensitive queries on org switch', async () => {
    const queryClient = new QueryClient();
    const { rerender } = render(
      <QueryClientProvider client={queryClient}>
        <MockAuthProvider organizationContext={{ id: 'org_001', name: 'Acme' }}>
          <Probe orgId="org_001" />
        </MockAuthProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('data')).toHaveTextContent('users-of-org_001'));
    expect(queryClient.getQueryData(queryKeys.users('org_001'))).toBe('users-of-org_001');

    rerender(
      <QueryClientProvider client={queryClient}>
        <MockAuthProvider organizationContext={{ id: 'org_002', name: 'Example Inc' }}>
          <Probe orgId="org_002" />
        </MockAuthProvider>
      </QueryClientProvider>,
    );

    // Must fail if org_001 data remains servable after the switch.
    await waitFor(() => expect(queryClient.getQueryData(queryKeys.users('org_001'))).toBeUndefined());
  });
});
