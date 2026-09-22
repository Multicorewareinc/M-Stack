import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import * as React from 'react';
import { describe, expect, it } from 'vitest';
import { useOrganizations } from './useOrganizations';
import { useCreateOrganization } from './useCreateOrganization';

describe('useCreateOrganization', () => {
  it('invalidates organizations list on success', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );

    const list = renderHook(() => useOrganizations(), { wrapper });
    await waitFor(() => expect(list.result.current.isSuccess).toBe(true));
    const initialCount = list.result.current.data!.length;

    const create = renderHook(() => useCreateOrganization(), { wrapper });
    await create.result.current.mutateAsync({ name: 'New Co', plan_id: 'plan_free' });

    // Must fail if the list query is left stale after a successful create.
    await waitFor(() => expect(list.result.current.data!.length).toBe(initialCount + 1));
  });
});
