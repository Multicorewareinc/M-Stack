import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { mockOrganizations, mockUserDirectory } from '../../mocks/fixtures';
import { CreateUserDrawer } from './CreateUserDrawer';

function renderDrawer() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onOpenChange = () => {};
  render(
    <QueryClientProvider client={queryClient}>
      <CreateUserDrawer open onOpenChange={onOpenChange} organizations={mockOrganizations} />
    </QueryClientProvider>,
  );
}

async function fillValidForm() {
  await userEvent.selectOptions(screen.getByLabelText('Organization'), mockOrganizations[1].name); // org_002, no existing users
  await userEvent.type(screen.getByLabelText('Username'), 'newuser');
  await userEvent.type(screen.getByLabelText('Email'), 'newuser@example.com');
  await userEvent.type(screen.getByLabelText('Temporary password'), 'TemporaryPass123!');
}

describe('CreateUserDrawer', () => {
  it('blocks submission without an organization', async () => {
    renderDrawer();
    await userEvent.type(screen.getByLabelText('Username'), 'newuser');
    await userEvent.type(screen.getByLabelText('Email'), 'newuser@example.com');
    await userEvent.type(screen.getByLabelText('Temporary password'), 'TemporaryPass123!');
    await userEvent.click(screen.getByRole('button', { name: 'Create User' }));
    // Must fail if the request fires anyway.
    expect(await screen.findByText('Organization is required.')).toBeInTheDocument();
  });

  it('blocks submission with a password shorter than the policy minimum', async () => {
    renderDrawer();
    await userEvent.selectOptions(screen.getByLabelText('Organization'), mockOrganizations[1].name);
    await userEvent.type(screen.getByLabelText('Username'), 'newuser');
    await userEvent.type(screen.getByLabelText('Email'), 'newuser@example.com');
    await userEvent.type(screen.getByLabelText('Temporary password'), 'short');
    await userEvent.click(screen.getByRole('button', { name: 'Create User' }));
    // Must fail if a sub-policy password is silently accepted (matches org-control-plane's
    // password_min_length=12).
    expect(await screen.findByText('Temporary password must be at least 12 characters.')).toBeInTheDocument();
  });

  it('shows email conflict on the field and preserves input', async () => {
    renderDrawer();
    const existing = mockUserDirectory.find((u) => u.organization_id === mockOrganizations[0].id)!;
    await userEvent.selectOptions(screen.getByLabelText('Organization'), mockOrganizations[0].name);
    await userEvent.type(screen.getByLabelText('Username'), 'someoneelse');
    await userEvent.type(screen.getByLabelText('Email'), existing.email);
    await userEvent.type(screen.getByLabelText('Temporary password'), 'TemporaryPass123!');
    await userEvent.click(screen.getByRole('button', { name: 'Create User' }));

    // Must fail if the error is generic or the form clears.
    expect(await screen.findByText(/already exists in this organization/)).toBeInTheDocument();
    expect(screen.getByLabelText('Email')).toHaveValue(existing.email);
  });

  it('creates the user and clears the form on success', async () => {
    renderDrawer();
    await fillValidForm();
    await userEvent.click(screen.getByRole('button', { name: 'Create User' }));

    // Must fail if the form does not reset (still-dirty state would leave stale values behind).
    await waitFor(() => expect(screen.getByLabelText('Username')).toHaveValue(''));
  });
});
