import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { mockOrganizations, mockPlans } from '../../mocks/fixtures';
import { ChangePlanDrawer } from './ChangePlanDrawer';
import { useOrganization } from './hooks/useOrganization';
import { useOrganizations } from './hooks/useOrganizations';

async function selectNewPlan(label: string) {
  await userEvent.selectOptions(screen.getByLabelText('New plan'), label);
}

/** Mounts the drawer alongside probes for the detail + list queries in one
 * tree — the same arrangement a real page (detail page + drawer) would use —
 * so invalidation-triggered refetches are observed exactly as production
 * React Query behavior would produce them. */
function DetailProbe({ id }: { id: string }) {
  const { data } = useOrganization(id);
  return <div data-testid="detail-plan">{data?.plan_id}</div>;
}
function ListProbe() {
  const { data } = useOrganizations();
  return <div data-testid="list-plan">{data?.find((o) => o.id === 'org_001')?.plan_id}</div>;
}

describe('ChangePlanDrawer', () => {
  it('shows current and new plan with entitlement statement', async () => {
    const queryClient = new QueryClient();
    const org = mockOrganizations[0]; // Acme Corp, ACTIVE, plan_pro

    render(
      <QueryClientProvider client={queryClient}>
        <ChangePlanDrawer open onOpenChange={() => {}} organization={org} plans={mockPlans} />
      </QueryClientProvider>,
    );

    expect(screen.getByTestId('current-plan-value')).toHaveTextContent('Pro');

    await selectNewPlan('Free');

    // Must fail if the summary omits the current or new plan.
    const summary = screen.getByTestId('plan-change-summary');
    expect(within(summary).getByText('Pro')).toBeInTheDocument();
    expect(within(summary).getByText('Free')).toBeInTheDocument();
    expect(summary).toHaveTextContent(/platform entitlement/i);
  });

  it('invalidates detail and list on success', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const org = mockOrganizations[0];

    render(
      <QueryClientProvider client={queryClient}>
        <DetailProbe id={org.id} />
        <ListProbe />
        <ChangePlanDrawer open onOpenChange={() => {}} organization={org} plans={mockPlans} />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(screen.getByTestId('detail-plan')).toHaveTextContent('plan_pro'));
    await waitFor(() => expect(screen.getByTestId('list-plan')).toHaveTextContent('plan_pro'));

    await selectNewPlan('Free');
    await userEvent.click(screen.getByRole('button', { name: 'Update Plan' }));

    // Must fail if either query is left stale.
    await waitFor(() => expect(screen.getByTestId('detail-plan')).toHaveTextContent('plan_free'));
    await waitFor(() => expect(screen.getByTestId('list-plan')).toHaveTextContent('plan_free'));
  });
});
