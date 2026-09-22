import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { MockAuthProvider } from '../../auth/MockAuthProvider';
import { mockPermissions } from '../../mocks/fixtures';
import { PermissionFormDrawer } from './PermissionFormDrawer';

function renderDrawer(permission?: (typeof mockPermissions)[number]) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MockAuthProvider>
        <PermissionFormDrawer open onOpenChange={() => {}} permission={permission} />
      </MockAuthProvider>
    </QueryClientProvider>,
  );
}

describe('PermissionFormDrawer', () => {
  it('blocks submission without a resource', async () => {
    renderDrawer();
    await userEvent.type(screen.getByLabelText('Action'), 'read');
    await userEvent.click(screen.getByRole('button', { name: 'Create Permission' }));
    // Must fail if the request fires anyway.
    expect(await screen.findByText('Resource is required.')).toBeInTheDocument();
  });

  it('shows a live slug preview computed from resource + action', async () => {
    renderDrawer();
    await userEvent.type(screen.getByLabelText('Resource'), 'users');
    await userEvent.type(screen.getByLabelText('Action'), 'read');
    // Must fail if the slug is a directly-editable input instead of a server-computed preview.
    expect(screen.queryByLabelText('Slug')).toBeNull();
    expect(await screen.findByText('users.read')).toBeInTheDocument();
  });

  it('shows conflict on the field and preserves input', async () => {
    renderDrawer();
    await userEvent.type(screen.getByLabelText('Resource'), 'users');
    await userEvent.type(screen.getByLabelText('Action'), 'read'); // users.read already taken
    await userEvent.click(screen.getByRole('button', { name: 'Create Permission' }));

    // Must fail if the error is generic or the form clears.
    expect(await screen.findByText("Permission 'users.read' already exists")).toBeInTheDocument();
    expect(screen.getByLabelText('Resource')).toHaveValue('users');
    expect(screen.getByLabelText('Action')).toHaveValue('read');
  });

  it('pre-fills current values in edit mode', () => {
    const perm = mockPermissions.find((p) => p.slug === 'users.read')!;
    renderDrawer(perm);
    // Must fail if a field is blank instead of the current value.
    expect(screen.getByLabelText('Resource')).toHaveValue('users');
    expect(screen.getByLabelText('Action')).toHaveValue('read');
    expect(screen.getByText('users.read')).toBeInTheDocument();
  });
});
