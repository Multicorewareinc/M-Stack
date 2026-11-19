import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { getUserRoles } from '../../api/users';
import { MockAuthProvider } from '../../auth/MockAuthProvider';
import { CreateUserDrawer } from './CreateUserDrawer';

function renderDrawer() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MockAuthProvider permissions={['users.create']}>
        <CreateUserDrawer open onOpenChange={() => {}} />
      </MockAuthProvider>
    </QueryClientProvider>,
  );
}

describe('CreateUserDrawer', () => {
  it('offers role selection in the create form', async () => {
    renderDrawer();
    // Must fail if role selection isn't offered — POST /v1/users assigns no default role, so a
    // user created here without this would be silently role-less until someone remembers to open
    // Manage Roles.
    expect(await screen.findByText('Admin')).toBeInTheDocument();
    expect(screen.getByText('Developer')).toBeInTheDocument();
  });

  it('blocks an invalid email', async () => {
    renderDrawer();
    await userEvent.type(screen.getByLabelText('Username'), 'newuser');
    await userEvent.type(screen.getByLabelText('Email'), 'not-an-email');
    await userEvent.click(screen.getByRole('button', { name: 'Create User' }));
    // Must fail if the request fires anyway.
    expect(await screen.findByText('Enter a valid email address.')).toBeInTheDocument();
  });

  it('creates the user and assigns the selected role in one submit', async () => {
    renderDrawer();
    await userEvent.type(screen.getByLabelText('Username'), 'newuser');
    await userEvent.type(screen.getByLabelText('Email'), 'newuser@example.com');
    await userEvent.type(screen.getByLabelText(/Temporary Password/), 'Sup3rSecret!!');
    await screen.findByText('Admin'); // roles loaded

    await userEvent.selectOptions(screen.getByLabelText('Role (optional)'), 'Admin');
    await userEvent.click(screen.getByRole('button', { name: 'Create User' }));

    // Must fail if the user is created but the role is never attached (the two calls aren't
    // actually sequenced), or if an unselected role leaks in.
    await waitFor(async () => {
      const { role_ids } = await getUserRoles('usr_newuser');
      expect(role_ids).toEqual(['role_admin']);
    });
  });

  it('only allows a single role to be selected, never several', async () => {
    renderDrawer();
    await screen.findByText('Admin');
    // A native single-value <select> can't hold two selections at once — this is the mechanism
    // that enforces "one role per user", not just a UI convention on top of a multi-select.
    const select = screen.getByLabelText('Role (optional)') as HTMLSelectElement;
    expect(select.multiple).toBe(false);
  });

  it('creates the user with no roles when none are selected', async () => {
    renderDrawer();
    await userEvent.type(screen.getByLabelText('Username'), 'roleless');
    await userEvent.type(screen.getByLabelText('Email'), 'roleless@example.com');
    await userEvent.type(screen.getByLabelText(/Temporary Password/), 'Sup3rSecret!!');
    await screen.findByText('Admin');

    await userEvent.click(screen.getByRole('button', { name: 'Create User' }));

    // Must fail if skipping role selection somehow still attaches something.
    await waitFor(async () => {
      const { role_ids } = await getUserRoles('usr_roleless');
      expect(role_ids).toEqual([]);
    });
  });

  it('requires a temporary password', async () => {
    renderDrawer();
    await userEvent.type(screen.getByLabelText('Username'), 'nopass');
    await userEvent.type(screen.getByLabelText('Email'), 'nopass@example.com');
    await screen.findByText('Admin');

    await userEvent.click(screen.getByRole('button', { name: 'Create User' }));

    // Must fail if the request fires anyway with an empty password.
    expect(await screen.findByText('Temporary password must be at least 12 characters.')).toBeInTheDocument();
  });

  it('rejects a temporary password under 12 characters', async () => {
    renderDrawer();
    await userEvent.type(screen.getByLabelText('Username'), 'shortpass');
    await userEvent.type(screen.getByLabelText('Email'), 'shortpass@example.com');
    await userEvent.type(screen.getByLabelText(/Temporary Password/), 'short1');
    await userEvent.click(screen.getByRole('button', { name: 'Create User' }));

    expect(await screen.findByText('Temporary password must be at least 12 characters.')).toBeInTheDocument();
  });

  it('accepts a valid temporary password and creates the user', async () => {
    renderDrawer();
    await userEvent.type(screen.getByLabelText('Username'), 'withpass');
    await userEvent.type(screen.getByLabelText('Email'), 'withpass@example.com');
    await userEvent.type(screen.getByLabelText(/Temporary Password/), 'Sup3rSecret!!');
    await screen.findByText('Admin');
    await userEvent.click(screen.getByRole('button', { name: 'Create User' }));

    // Must fail if the valid password is somehow still rejected by client-side validation.
    await waitFor(async () => {
      const { role_ids } = await getUserRoles('usr_withpass');
      expect(role_ids).toEqual([]);
    });
  });
});
