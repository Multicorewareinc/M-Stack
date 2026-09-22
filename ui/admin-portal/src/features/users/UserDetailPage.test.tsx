import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { delay, http, HttpResponse } from 'msw';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { mockUserDetail } from '../../mocks/fixtures';
import { server } from '../../mocks/server';
import { UserDetailPage } from './UserDetailPage';

function renderDetail(userId: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/users/${userId}`]}>
        <Routes>
          <Route path="/users/:userId" element={<UserDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('UserDetailPage', () => {
  it('shows the authoritative-fetch loading state', async () => {
    server.use(
      http.get('/v1/users/:id', async () => {
        await delay(50);
        return HttpResponse.json(mockUserDetail);
      }),
    );
    renderDetail(mockUserDetail.id);
    // Must fail if the generic directory skeleton renders instead, or no
    // loading indication appears.
    expect(screen.getByTestId('user-detail-loading')).toHaveTextContent('Loading latest user information…');
    await screen.findByRole('heading', { name: 'John Smith' });
  });

  it('renders Overview, Roles, Effective Permissions tabs', async () => {
    renderDetail(mockUserDetail.id);
    expect(await screen.findByRole('heading', { name: 'John Smith' })).toBeInTheDocument();
    // Must fail if a tab is missing or labeled plain "Permissions".
    expect(screen.getByRole('tab', { name: 'Overview' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Roles' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Effective Permissions' })).toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: /^Permissions$/ })).toBeNull();
  });

  it('renders not-found for a missing user', async () => {
    renderDetail('usr_does_not_exist');
    // Must fail if the page crashes or renders nothing.
    expect(await screen.findByText('User not found')).toBeInTheDocument();
  });
});
