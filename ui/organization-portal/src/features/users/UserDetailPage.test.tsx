import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { MockAuthProvider } from '../../auth/MockAuthProvider';
import { UserDetailPage } from './UserDetailPage';

function renderDetail(userId: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MockAuthProvider>
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
});
