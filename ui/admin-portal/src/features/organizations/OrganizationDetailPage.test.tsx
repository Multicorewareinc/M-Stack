import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { OrganizationDetailPage } from './OrganizationDetailPage';

function UserDetailStub() {
  return <div>User detail stub page</div>;
}

function renderDetail(id: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/organizations/${id}`]}>
        <Routes>
          <Route path="/organizations/:id" element={<OrganizationDetailPage />} />
          <Route path="/users/:userId" element={<UserDetailStub />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('OrganizationDetailPage', () => {
  it('renders Overview, Users, Configuration tabs', async () => {
    renderDetail('org_001');
    expect(await screen.findByRole('heading', { name: 'Acme Corporation' })).toBeInTheDocument();
    // Must fail if a tab is missing.
    expect(screen.getByRole('tab', { name: 'Overview' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Users' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Configuration' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute('aria-selected', 'true');
  });

  it('users tab renders via the shared directory table', async () => {
    renderDetail('org_001');
    await screen.findByRole('heading', { name: 'Acme Corporation' });
    await userEvent.click(screen.getByRole('tab', { name: 'Users' }));
    const row = await screen.findByText('John Smith');
    // Row-click navigation is a UserDirectoryTable behavior the SP-03
    // placeholder table never had — proves the shared component renders here.
    // Must fail if the tab renders a different, divergent table implementation.
    await userEvent.click(row);
    expect(await screen.findByText('User detail stub page')).toBeInTheDocument();
  });

  it('renders not-found for a missing organization', async () => {
    renderDetail('org_does_not_exist');
    // Must fail if the page crashes or renders nothing.
    expect(await screen.findByText('Organization not found')).toBeInTheDocument();
  });
});
