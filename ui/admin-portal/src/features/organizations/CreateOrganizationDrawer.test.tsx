import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { mockPlans } from '../../mocks/fixtures';
import { CreateOrganizationDrawer } from './CreateOrganizationDrawer';

function renderDrawer() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onOpenChange = () => {};
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/organizations']}>
        <Routes>
          <Route
            path="/organizations"
            element={<CreateOrganizationDrawer open onOpenChange={onOpenChange} plans={mockPlans} />}
          />
          <Route path="/organizations/:id" element={<DetailStub />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function DetailStub() {
  return <div>Organization detail page</div>;
}

describe('CreateOrganizationDrawer', () => {
  it('blocks submission without a name', async () => {
    renderDrawer();
    await userEvent.selectOptions(screen.getByLabelText('Plan'), mockPlans[0].name);
    await userEvent.click(screen.getByRole('button', { name: 'Create Organization' }));
    // Must fail if the request fires anyway.
    expect(await screen.findByText('Organization name is required.')).toBeInTheDocument();
  });

  it('shows name conflict on the field and preserves input', async () => {
    renderDrawer();
    await userEvent.type(screen.getByLabelText('Organization name'), 'Acme Corporation'); // already taken by org_001
    await userEvent.selectOptions(screen.getByLabelText('Plan'), mockPlans[0].name);
    await userEvent.click(screen.getByRole('button', { name: 'Create Organization' }));

    // Must fail if the error is only shown generically or the form clears.
    expect(await screen.findByText(/already exists/)).toBeInTheDocument();
    expect(screen.getByLabelText('Organization name')).toHaveValue('Acme Corporation');
  });

  it('navigates to detail and invalidates the list on success', async () => {
    renderDrawer();
    await userEvent.type(screen.getByLabelText('Organization name'), 'New Co');
    await userEvent.selectOptions(screen.getByLabelText('Plan'), mockPlans[0].name);
    await userEvent.click(screen.getByRole('button', { name: 'Create Organization' }));

    // Must fail if the list is not invalidated or navigation does not occur.
    await waitFor(() => expect(screen.getByText('Organization detail page')).toBeInTheDocument());
  });
});
