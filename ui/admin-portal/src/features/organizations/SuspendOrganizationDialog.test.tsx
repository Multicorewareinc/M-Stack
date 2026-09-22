import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { getOrganization } from '../../api/organizations';
import { mockOrganizations } from '../../mocks/fixtures';
import { SuspendOrganizationDialog } from './SuspendOrganizationDialog';

describe('SuspendOrganizationDialog', () => {
  it('requires confirmation before suspending', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const org = mockOrganizations[0]; // active

    render(
      <QueryClientProvider client={queryClient}>
        <SuspendOrganizationDialog open onOpenChange={() => {}} organization={org} />
      </QueryClientProvider>,
    );

    // Dialog is open but the backend must not yet reflect a suspend.
    // Must fail if the request fires on menu selection alone (i.e. merely
    // rendering the dialog) rather than only on explicit confirmation.
    expect((await getOrganization(org.id)).status).toBe('active');

    await userEvent.click(screen.getByRole('button', { name: 'Suspend' }));

    await waitFor(async () => {
      expect((await getOrganization(org.id)).status).toBe('suspended');
    });
  });
});
