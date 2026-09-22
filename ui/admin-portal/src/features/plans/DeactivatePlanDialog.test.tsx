import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { listPlans } from '../../api/plans';
import { mockPlans } from '../../mocks/fixtures';
import { server } from '../../mocks/server';
import { DeactivatePlanDialog } from './DeactivatePlanDialog';

describe('DeactivatePlanDialog', () => {
  it('soft-deactivates via PATCH, never DELETE', async () => {
    const deleteSpy = vi.fn();
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'DELETE') deleteSpy();
    });

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const pro = mockPlans.find((p) => p.name === 'Pro')!;

    render(
      <QueryClientProvider client={queryClient}>
        <DeactivatePlanDialog open onOpenChange={() => {}} plan={pro} />
      </QueryClientProvider>,
    );

    expect((await listPlans()).find((p) => p.id === pro.id)?.is_active).toBe(true);

    await userEvent.click(screen.getByRole('button', { name: 'Deactivate' }));

    await waitFor(async () => {
      expect((await listPlans()).find((p) => p.id === pro.id)?.is_active).toBe(false);
    });
    // Must fail if a DELETE request is issued or the plan disappears entirely.
    expect(deleteSpy).not.toHaveBeenCalled();
    expect((await listPlans()).find((p) => p.id === pro.id)).toBeDefined();
  });
});
