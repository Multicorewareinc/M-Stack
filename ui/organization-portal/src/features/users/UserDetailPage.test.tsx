import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { getUserRoles } from '../../api/users';
import { MockAuthProvider } from '../../auth/MockAuthProvider';
import { UserDetailPage } from './UserDetailPage';

function renderDetail(userId: string, permissions?: string[]) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MockAuthProvider permissions={permissions}>
        <MemoryRouter initialEntries={[`/users/${userId}`]}>
          <Routes>
            <Route path="/users/:userId" element={<UserDetailPage />} />
          </Routes>
        </MemoryRouter>
      </MockAuthProvider>
    </QueryClientProvider>,
  );
}

describe('UserDetailPage', () => {
  it('renders Profile, Roles, Effective Permissions tabs', async () => {
    renderDetail('usr_001');
    expect(await screen.findByRole('heading', { name: 'John Smith' })).toBeInTheDocument();
    // Must fail if a tab is missing or mislabeled.
    expect(screen.getByRole('tab', { name: 'Profile' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Roles' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Effective Permissions' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Activity' })).toBeInTheDocument();
  });

  it('renders not-found for a missing user', async () => {
    renderDetail('usr_does_not_exist');
    // Must fail if the page crashes or renders nothing.
    expect(await screen.findByText('User not found')).toBeInTheDocument();
  });

  it('shows the current role read-only until Edit Role is clicked', async () => {
    renderDetail('usr_001');
    await screen.findByRole('heading', { name: 'John Smith' });
    await userEvent.click(screen.getByRole('tab', { name: 'Roles' }));

    // Must fail if the dropdown is accessible before Edit Role is clicked, or if a separate
    // "Manage Roles" drawer trigger still exists instead of this inline flow.
    expect(await screen.findByText('Admin')).toBeInTheDocument();
    expect(screen.queryByLabelText('Role')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Manage Roles' })).toBeNull();

    await userEvent.click(screen.getByRole('button', { name: 'Edit Role' }));

    const select = (await screen.findByLabelText('Role')) as HTMLSelectElement;
    expect(select.value).toBe('role_admin');
    // Save starts disabled — nothing changed yet.
    expect(screen.getByRole('button', { name: 'Save Roles' })).toBeDisabled();
  });

  it('changes the user role after Edit Role, then saves and returns to read-only', async () => {
    renderDetail('usr_001');
    await screen.findByRole('heading', { name: 'John Smith' });
    await userEvent.click(screen.getByRole('tab', { name: 'Roles' }));
    await userEvent.click(screen.getByRole('button', { name: 'Edit Role' }));

    const select = await screen.findByLabelText('Role');
    await userEvent.selectOptions(select, 'Developer');
    await userEvent.click(screen.getByRole('button', { name: 'Save Roles' }));

    // Must fail if the change never reaches the backend, or a second role gets added instead of
    // replacing the first — a user has exactly one role.
    await waitFor(async () => {
      const { role_ids } = await getUserRoles('usr_001');
      expect(role_ids).toEqual(['role_developer']);
    });
    // Must fail if the form stays open in edit mode after a successful save.
    await waitFor(() => expect(screen.queryByLabelText('Role')).toBeNull());
    expect(screen.getByText('Developer')).toBeInTheDocument();
  });

  it('Cancel discards the pick and leaves edit mode without saving', async () => {
    renderDetail('usr_001');
    await screen.findByRole('heading', { name: 'John Smith' });
    await userEvent.click(screen.getByRole('tab', { name: 'Roles' }));
    await userEvent.click(screen.getByRole('button', { name: 'Edit Role' }));

    await userEvent.selectOptions(screen.getByLabelText('Role'), 'Developer');
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    // Must fail if Cancel still persisted the pick, or left the dropdown open.
    expect(screen.queryByLabelText('Role')).toBeNull();
    expect(screen.getByText('Admin')).toBeInTheDocument();
    const { role_ids } = await getUserRoles('usr_001');
    expect(role_ids).toEqual(['role_admin']);
  });

  it('shows the role read-only, with no Edit Role button, without users.update', async () => {
    renderDetail('usr_001', ['users.read']);
    await screen.findByRole('heading', { name: 'John Smith' });
    await userEvent.click(screen.getByRole('tab', { name: 'Roles' }));

    // Must fail if the edit control renders regardless of permission.
    expect(await screen.findByText('Admin')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit Role' })).toBeNull();
    expect(screen.queryByLabelText('Role')).toBeNull();
  });
});
