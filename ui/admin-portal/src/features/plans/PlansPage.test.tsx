import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { PlansPage } from './PlansPage';

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <PlansPage />
    </QueryClientProvider>,
  );
}

describe('PlansPage', () => {
  it('renders seeded plans with limits and status', async () => {
    renderPage();
    expect(await screen.findByText('Pro')).toBeInTheDocument();
    expect(screen.getByText('Free')).toBeInTheDocument();

    // Scoped per row: Free's quota (100K) and Pro's TPM (100K) format
    // identically, so a bare getByText('100K') would be ambiguous.
    const rows = screen.getAllByRole('row');
    const proRow = rows.find((r) => within(r).queryByText('Pro'));
    expect(proRow).toBeDefined();
    // Must fail if a column or value is wrong.
    expect(within(proRow!).getByText('100K')).toBeInTheDocument(); // TPM
    expect(within(proRow!).getByText('600')).toBeInTheDocument(); // RPM
    expect(within(proRow!).getByText('1M')).toBeInTheDocument(); // Quota

    expect(screen.getAllByText('Active').length).toBeGreaterThan(0);
  });

  it('hides Deactivate for an already-inactive plan', async () => {
    renderPage();
    await screen.findByText('Legacy');
    expect(screen.getByText('Inactive')).toBeInTheDocument();

    const rows = screen.getAllByRole('row');
    const legacyRow = rows.find((r) => within(r).queryByText('Legacy'));
    expect(legacyRow).toBeDefined();

    const menuButton = within(legacyRow!).getByRole('button', { name: 'Row actions' });
    await userEvent.click(menuButton);
    const menu = await screen.findByRole('menu');
    // Must fail if Deactivate appears twice-actionable on an inactive plan.
    expect(within(menu).getByRole('menuitem', { name: 'Edit' })).toBeInTheDocument();
    expect(within(menu).queryByRole('menuitem', { name: 'Deactivate' })).toBeNull();
  });
});
