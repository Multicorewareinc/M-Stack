import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { MockAuthProvider } from '../../auth/MockAuthProvider';
import { mockUsers } from '../../mocks/fixtures';
import { EditUserDrawer } from './EditUserDrawer';

describe('EditUserDrawer', () => {
  it('omits immutable identifier fields', () => {
    const queryClient = new QueryClient();
    const user = mockUsers[0];
    render(
      <QueryClientProvider client={queryClient}>
        <MockAuthProvider>
          <EditUserDrawer open onOpenChange={() => {}} user={user} />
        </MockAuthProvider>
      </QueryClientProvider>,
    );
    // Must fail if username is rendered as an editable input.
    expect(screen.queryByLabelText('Username')).toBeNull();
    expect(screen.getByText(user.username)).toBeInTheDocument(); // shown, not editable
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
  });
});
