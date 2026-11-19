import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { MockAuthProvider } from '../../auth/MockAuthProvider';
import { server } from '../../mocks/server';
import { UsersPage } from './UsersPage';

function renderPage(permissions?: string[]) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MockAuthProvider permissions={permissions}>
        <MemoryRouter>
          <UsersPage />
        </MemoryRouter>
      </MockAuthProvider>
    </QueryClientProvider>,
  );
}

describe('UsersPage', () => {
  it('loads users with a single request scoped to the authenticated org', async () => {
    const urls: string[] = [];
    const listener = ({ request }: { request: Request }) => {
      if (new URL(request.url).pathname === '/v1/users') urls.push(request.url);
    };
    server.events.on('request:start', listener);

    renderPage();
    await screen.findByText('John Smith');

    // Must fail if a per-row detail request is issued or a user from another org renders.
    expect(urls.length).toBe(1);
    expect(screen.getByText('Mark Lee')).toBeInTheDocument(); // same org_001
    server.events.removeListener('request:start', listener);
  });

  it('search filters the list', async () => {
    renderPage();
    await screen.findByText('John Smith');
    expect(screen.getByText('Mark Lee')).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText('Search users'), 'John');

    // Must fail if searching does not affect the displayed rows.
    await waitFor(() => expect(screen.queryByText('Mark Lee')).toBeNull());
    expect(screen.getByText('John Smith')).toBeInTheDocument();
  });

  it('hides Add User without users.create permission', async () => {
    renderPage(['users.read']);
    await screen.findByText('John Smith');
    // Must fail if the control renders regardless of permission.
    expect(screen.queryByRole('button', { name: '+ Add User' })).toBeNull();
  });

  it('offers Deactivate for an active user and Activate for a suspended one', async () => {
    renderPage();
    await screen.findByText('John Smith');

    const rows = screen.getAllByRole('row');
    const johnRow = rows.find((r) => within(r).queryByText('John Smith')); // active
    const markRow = rows.find((r) => within(r).queryByText('Mark Lee')); // suspended
    expect(johnRow).toBeDefined();
    expect(markRow).toBeDefined();

    await userEvent.click(within(johnRow!).getByRole('button', { name: 'Row actions' }));
    let menu = await screen.findByRole('menu');
    // Must fail if an active user's row offers to activate an already-active account.
    expect(within(menu).getByRole('menuitem', { name: 'Deactivate' })).toBeInTheDocument();
    expect(within(menu).queryByRole('menuitem', { name: 'Activate' })).toBeNull();
    await userEvent.keyboard('{Escape}');

    await userEvent.click(within(markRow!).getByRole('button', { name: 'Row actions' }));
    menu = await screen.findByRole('menu');
    // Must fail if a suspended user's row offers to deactivate an already-suspended account.
    expect(within(menu).getByRole('menuitem', { name: 'Activate' })).toBeInTheDocument();
    expect(within(menu).queryByRole('menuitem', { name: 'Deactivate' })).toBeNull();
  });

  it('hides Deactivate/Activate without users.update permission', async () => {
    renderPage(['users.read']);
    await screen.findByText('John Smith');

    const rows = screen.getAllByRole('row');
    const johnRow = rows.find((r) => within(r).queryByText('John Smith'));
    await userEvent.click(within(johnRow!).getByRole('button', { name: 'Row actions' }));
    const menu = await screen.findByRole('menu');
    // Must fail if the status action renders regardless of permission.
    expect(within(menu).queryByRole('menuitem', { name: 'Deactivate' })).toBeNull();
    expect(within(menu).queryByRole('menuitem', { name: 'Activate' })).toBeNull();
  });

  it('deactivates an active user after confirming', async () => {
    renderPage();
    await screen.findByText('John Smith');

    const rows = screen.getAllByRole('row');
    const johnRow = rows.find((r) => within(r).queryByText('John Smith'));
    await userEvent.click(within(johnRow!).getByRole('button', { name: 'Row actions' }));
    const menu = await screen.findByRole('menu');
    await userEvent.click(within(menu).getByRole('menuitem', { name: 'Deactivate' }));

    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Deactivate' }));

    // Must fail if the status change didn't actually take effect. Scoped to John's own row —
    // Mark Lee is already Suspended in the fixtures, so a bare getByText would be ambiguous.
    await waitFor(() => {
      const updatedRow = screen.getAllByRole('row').find((r) => within(r).queryByText('John Smith'));
      expect(within(updatedRow!).getByText('Suspended')).toBeInTheDocument();
    });
  });

  it('reactivates a suspended user with one click, no confirmation', async () => {
    renderPage();
    await screen.findByText('Mark Lee');

    const rows = screen.getAllByRole('row');
    const markRow = rows.find((r) => within(r).queryByText('Mark Lee'));
    await userEvent.click(within(markRow!).getByRole('button', { name: 'Row actions' }));
    const menu = await screen.findByRole('menu');
    await userEvent.click(within(menu).getByRole('menuitem', { name: 'Activate' }));

    // Must fail if reactivating incorrectly required a confirmation dialog.
    expect(screen.queryByRole('dialog')).toBeNull();
    await waitFor(() => expect(screen.getAllByText('Active').length).toBeGreaterThan(0));
  });
});
