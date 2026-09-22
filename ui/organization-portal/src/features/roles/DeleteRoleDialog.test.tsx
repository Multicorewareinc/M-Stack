import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { MockAuthProvider } from '../../auth/MockAuthProvider';
import { mockRoles } from '../../mocks/fixtures';
import { DeleteRoleDialog } from './DeleteRoleDialog';

describe('DeleteRoleDialog', () => {
  it('surfaces the user-assignment conflict message', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // role_admin is assigned to usr_001 in the seed fixtures.
    const role = mockRoles.find((r) => r.id === 'role_admin')!;

    render(
      <QueryClientProvider client={queryClient}>
        <MockAuthProvider>
          <DeleteRoleDialog open onOpenChange={() => {}} role={role} />
        </MockAuthProvider>
      </QueryClientProvider>,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Delete' }));

    // Must fail if a generic/blank error renders instead.
    expect(
      await screen.findByText('This role cannot be deleted while it is assigned to users.'),
    ).toBeInTheDocument();
  });
});
