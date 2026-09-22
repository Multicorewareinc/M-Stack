import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { MockAuthProvider } from '../../auth/MockAuthProvider';
import { RoleFormDrawer } from './RoleFormDrawer';


function renderDrawer() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MockAuthProvider>
        <RoleFormDrawer open onOpenChange={() => {}} />
      </MockAuthProvider>
    </QueryClientProvider>,
  );
}

describe('RoleFormDrawer', () => {
  it('blocks submission without a name', async () => {
    renderDrawer();
    // Touch a different field to make the form dirty (EntityDrawer's submit
    // button is disabled until dirty) while leaving Name empty.
    await userEvent.type(screen.getByLabelText('Description'), 'Some description');
    await userEvent.click(screen.getByRole('button', { name: 'Create Role' }));
    // Must fail if the request fires anyway.
    expect(await screen.findByText('Role name is required.')).toBeInTheDocument();
  });
});
