import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import * as React from 'react';
import { describe, expect, it } from 'vitest';
import { useCreatePlan } from './useCreatePlan';
import { usePlans } from './usePlans';

describe('useCreatePlan', () => {
  it('invalidates plans list on success', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );

    const list = renderHook(() => usePlans(), { wrapper });
    await waitFor(() => expect(list.result.current.isSuccess).toBe(true));
    const initialCount = list.result.current.data!.length;

    const create = renderHook(() => useCreatePlan(), { wrapper });
    await create.result.current.mutateAsync({
      name: 'Startup',
      tpm: 5000,
      rpm: 30,
      quota_monthly_tokens: 50000,
    });

    // Must fail if the list is left stale.
    await waitFor(() => expect(list.result.current.data!.length).toBe(initialCount + 1));
  });
});
