import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { getUserRoles } from '../../api/users';
import { MockAuthProvider } from '../../auth/MockAuthProvider';
import { ManageRolesDrawer } from './ManageRolesDrawer';

function renderDrawer() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MockAuthProvider>
        <ManageRolesDrawer open onOpenChange={() => {}} userId="usr_001" />
      </MockAuthProvider>
    </QueryClientProvider>,
  );
}

describe('ManageRolesDrawer', () => {
  it('pre-checks current roles and saves the updated set', async () => {
    renderDrawer();

    // usr_001 currently has role_admin only.
    const adminCheckbox = await screen.findByLabelText('Admin');
    const developerCheckbox = screen.getByLabelText('Developer');
    // Must fail if the current roles aren't pre-checked.
    expect(adminCheckbox).toBeChecked();
    expect(developerCheckbox).not.toBeChecked();

    await userEvent.click(developerCheckbox);
    await userEvent.click(screen.getByRole('button', { name: 'Save Roles' }));

    // Must fail if the save omits the change.
    await waitFor(async () => {
      const { role_ids } = await getUserRoles('usr_001');
      expect(new Set(role_ids)).toEqual(new Set(['role_admin', 'role_developer']));
    });
  });
});
