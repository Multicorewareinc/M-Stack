import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import * as React from 'react';
import { describe, expect, it } from 'vitest';
import { provisioningRefetchInterval, useProvisioningStatus } from './useProvisioningStatus';

describe('provisioningRefetchInterval (pure)', () => {
  it('never polls once a terminal state is reached', () => {
    // Must fail if polling continues past a terminal state.
    expect(provisioningRefetchInterval('active')).toBe(false);
    expect(provisioningRefetchInterval('failed')).toBe(false);
    expect(provisioningRefetchInterval('provisioning')).toBeGreaterThan(0);
    expect(provisioningRefetchInterval(undefined)).toBe(false);
  });
});

describe('useProvisioningStatus', () => {
  it('reflects transition to ACTIVE and stops polling', async () => {
    // org_002 is seeded PROVISIONING; the MSW handler flips it to ACTIVE
    // deterministically after a fixed number of GET calls (handlers.ts).
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useProvisioningStatus('org_002'), { wrapper });

    await waitFor(() => expect(result.current.data?.status).toBe('provisioning'));

    await result.current.refetch();
    await result.current.refetch();

    // Must fail if the UI keeps showing PROVISIONING after the backend
    // reports ACTIVE.
    await waitFor(() => expect(result.current.data?.status).toBe('active'));
    expect(provisioningRefetchInterval(result.current.data?.status)).toBe(false);
  });

  it('offers retry only when backend marks retryable', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );

    const retryable = renderHook(() => useProvisioningStatus('org_003'), { wrapper });
    await waitFor(() => expect(retryable.result.current.data?.status).toBe('failed'));
    expect(retryable.result.current.data?.retryable).toBe(true);

    const notRetryable = renderHook(() => useProvisioningStatus('org_004'), { wrapper });
    await waitFor(() => expect(notRetryable.result.current.data?.status).toBe('failed'));
    // Must fail if Retry appears regardless of the backend flag.
    expect(notRetryable.result.current.data?.retryable).toBe(false);
  });
});
