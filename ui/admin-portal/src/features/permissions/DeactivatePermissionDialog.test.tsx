import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { listPermissions } from '../../api/permissions';
import { mockPermissions } from '../../mocks/fixtures';
import { server } from '../../mocks/server';
import { DeactivatePermissionDialog } from './DeactivatePermissionDialog';

describe('DeactivatePermissionDialog', () => {
  it('soft-deactivates via PATCH, never DELETE', async () => {
    const deleteSpy = vi.fn();
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'DELETE') deleteSpy();
    });

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const perm = mockPermissions.find((p) => p.slug === 'users.create')!; // not role-referenced

    render(
      <QueryClientProvider client={queryClient}>
        <DeactivatePermissionDialog open onOpenChange={() => {}} permission={perm} />
      </QueryClientProvider>,
    );

    expect((await listPermissions()).find((p) => p.id === perm.id)?.is_active).toBe(true);

    await userEvent.click(screen.getByRole('button', { name: 'Deactivate' }));

    await waitFor(async () => {
      expect((await listPermissions()).find((p) => p.id === perm.id)?.is_active).toBe(false);
    });
    // Must fail if a DELETE request is issued or the permission disappears entirely.
    expect(deleteSpy).not.toHaveBeenCalled();
    expect((await listPermissions()).find((p) => p.id === perm.id)).toBeDefined();
  });

  it('surfaces the role-reference conflict message', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const perm = mockPermissions.find((p) => p.slug === 'users.read')!; // hardcoded as role-referenced

    render(
      <QueryClientProvider client={queryClient}>
        <DeactivatePermissionDialog open onOpenChange={() => {}} permission={perm} />
      </QueryClientProvider>,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Deactivate' }));

    // Must fail if a generic/blank error renders instead of the specific conflict message.
    expect(
      await screen.findByText('This permission cannot be removed because it is currently assigned to one or more roles.'),
    ).toBeInTheDocument();
    expect((await listPermissions()).find((p) => p.id === perm.id)?.is_active).toBe(true);
  });
});
