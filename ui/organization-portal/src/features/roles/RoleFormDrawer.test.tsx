import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { getRolePermissions } from '../../api/roles';
import type { Role } from '../../api/types';
import { MockAuthProvider } from '../../auth/MockAuthProvider';
import { RoleFormDrawer } from './RoleFormDrawer';


function renderDrawer(role?: Role) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MockAuthProvider>
        <RoleFormDrawer open onOpenChange={() => {}} role={role} />
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

  it('offers a permission catalog in create mode', async () => {
    renderDrawer();
    // Must fail if the create form has no permission-selection UI at all.
    expect(screen.getByLabelText('Search permissions')).toBeInTheDocument();
    expect(await screen.findByText('users')).toBeInTheDocument();
    expect(screen.getByText('roles')).toBeInTheDocument();
  });

  it('does not show permission selection in edit mode', async () => {
    renderDrawer({
      id: 'role_developer',
      organization_id: 'org_001',
      name: 'Developer',
      description: null,
      is_system_role: false,
      created_at: '',
      updated_at: '',
    });
    await screen.findByLabelText('Name');
    // Must fail if edit mode also renders the create-only permission picker — permissions for an
    // existing role are only ever composed on its own dedicated Permissions tab (§65-69).
    expect(screen.queryByLabelText('Search permissions')).toBeNull();
  });

  it('creates the role and attaches the selected permissions in one submit', async () => {
    renderDrawer();
    await userEvent.type(screen.getByLabelText('Name'), 'QA Role');
    await screen.findByText('roles'); // catalog loaded

    const rolesSection = screen.getByText('roles').closest('section')!;
    await userEvent.click(within(rolesSection).getByLabelText('read'));
    expect(screen.getByText('1 permissions selected')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Create Role' }));

    // Must fail if the role is created but its permissions are never attached (i.e. the two
    // calls aren't actually sequenced), or if an unrelated group's permission leaks in.
    await waitFor(async () => {
      const { permission_ids } = await getRolePermissions('role_qa_role');
      expect(permission_ids).toEqual(['perm_roles_read']);
    });
  });

  it('creates the role with no permissions when none are selected', async () => {
    renderDrawer();
    await userEvent.type(screen.getByLabelText('Name'), 'Empty Role');
    await screen.findByText('roles');

    await userEvent.click(screen.getByRole('button', { name: 'Create Role' }));

    // Must fail if skipping permission selection somehow still attaches something.
    await waitFor(async () => {
      const { permission_ids } = await getRolePermissions('role_empty_role');
      expect(permission_ids).toEqual([]);
    });
  });
});
