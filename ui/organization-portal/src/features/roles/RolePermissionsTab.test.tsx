import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { getRolePermissions } from '../../api/roles';
import { MockAuthProvider } from '../../auth/MockAuthProvider';
import { RolePermissionsTab } from './RolePermissionsTab';

function renderTab(roleId: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MockAuthProvider>
        <RolePermissionsTab roleId={roleId} />
      </MockAuthProvider>
    </QueryClientProvider>,
  );
}

describe('RolePermissionsTab', () => {
  it('groups permissions by resource', async () => {
    renderTab('role_developer');
    // Must fail if permissions render as one flat list.
    expect(await screen.findByText('users')).toBeInTheDocument();
    expect(screen.getByText('roles')).toBeInTheDocument();
  });

  it('search filters the catalog', async () => {
    renderTab('role_developer');
    await screen.findByText('users');
    expect(screen.getByText('roles')).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText('Search permissions'), 'roles.read');

    // Must fail if search does not affect the displayed permissions.
    await waitFor(() => expect(screen.queryByText('users')).toBeNull());
    expect(screen.getByText('roles')).toBeInTheDocument();
  });

  it('select-all affects only its resource group and submits the resulting selection', async () => {
    renderTab('role_developer'); // starts with only perm_users_read selected
    await screen.findByText('roles');

    const rolesGroup = screen.getByText('roles:').closest('div')!;
    await userEvent.click(within(rolesGroup).getByRole('button', { name: 'Select all' }));

    await userEvent.click(screen.getByRole('button', { name: 'Save Permissions' }));

    // Must fail if other groups' selections change or the saved payload diverges.
    await waitFor(async () => {
      const { permission_ids } = await getRolePermissions('role_developer');
      expect(new Set(permission_ids)).toEqual(new Set(['perm_users_read', 'perm_roles_read']));
    });
  });
});
