import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
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
  it('has no role-assignment field', () => {
    renderDrawer();
    // Must fail if a role control appears in the create form.
    expect(screen.queryByLabelText(/role/i)).toBeNull();
    expect(screen.queryByText(/roles/i)).toBeNull();
  });

  it('blocks an invalid email', async () => {
    renderDrawer();
    await userEvent.type(screen.getByLabelText('Username'), 'newuser');
    await userEvent.type(screen.getByLabelText('Email'), 'not-an-email');
    await userEvent.click(screen.getByRole('button', { name: 'Create User' }));
    // Must fail if the request fires anyway.
    expect(await screen.findByText('Enter a valid email address.')).toBeInTheDocument();
  });
});
