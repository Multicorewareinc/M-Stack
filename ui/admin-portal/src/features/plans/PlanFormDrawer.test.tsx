import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { mockPlans } from '../../mocks/fixtures';
import { PlanFormDrawer } from './PlanFormDrawer';

function renderDrawer(plan?: (typeof mockPlans)[number]) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <PlanFormDrawer open onOpenChange={() => {}} plan={plan} />
    </QueryClientProvider>,
  );
}

describe('PlanFormDrawer', () => {
  it('blocks a negative numeric limit', async () => {
    renderDrawer();
    await userEvent.type(screen.getByLabelText('Name'), 'Test Plan');
    // fireEvent.change sets the value atomically — typing "-5" char-by-char
    // via userEvent.type hits jsdom's number-input sanitization dropping the
    // lone "-" mid-keystroke, which would silently produce a valid "5".
    fireEvent.change(screen.getByLabelText('TPM Limit'), { target: { value: '-5' } });
    await userEvent.click(screen.getByRole('button', { name: 'Create Plan' }));
    // Must fail if the request fires anyway.
    expect(await screen.findByText('Must not be negative.')).toBeInTheDocument();
  });

  it('shows name conflict on the field and preserves input', async () => {
    renderDrawer();
    await userEvent.type(screen.getByLabelText('Name'), 'Pro'); // already taken
    await userEvent.type(screen.getByLabelText('TPM Limit'), '{selectall}1000');
    await userEvent.type(screen.getByLabelText('RPM Limit'), '{selectall}10');
    await userEvent.type(screen.getByLabelText('Quota'), '{selectall}10000');
    await userEvent.click(screen.getByRole('button', { name: 'Create Plan' }));

    // Must fail if the error is generic or the form clears.
    expect(await screen.findByText("Plan name 'Pro' already exists")).toBeInTheDocument();
    expect(screen.getByLabelText('Name')).toHaveValue('Pro');
  });

  it('pre-fills current values in edit mode', () => {
    const pro = mockPlans.find((p) => p.name === 'Pro')!;
    renderDrawer(pro);
    // Must fail if a field is blank instead of the current value.
    expect(screen.getByLabelText('Name')).toHaveValue('Pro');
    expect(screen.getByLabelText('TPM Limit')).toHaveValue(100000);
  });

  it('edit form shows exact numeric value, not compacted', () => {
    const pro = mockPlans.find((p) => p.name === 'Pro')!;
    renderDrawer(pro);
    // Must fail if the field shows "100K" instead of "100000".
    expect(screen.getByLabelText('TPM Limit')).toHaveValue(100000);
    expect(screen.queryByDisplayValue('100K')).toBeNull();
  });
});
