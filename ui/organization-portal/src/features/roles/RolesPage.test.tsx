import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { MockAuthProvider } from '../../auth/MockAuthProvider';
import { server } from '../../mocks/server';
import { RolesPage } from './RolesPage';

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MockAuthProvider>
        <MemoryRouter>
          <RolesPage />
        </MemoryRouter>
      </MockAuthProvider>
    </QueryClientProvider>,
  );
}

describe('RolesPage', () => {
  it('loads roles with a single request', async () => {
    const urls: string[] = [];
    const listener = ({ request }: { request: Request }) => {
      if (new URL(request.url).pathname === '/v1/roles') urls.push(request.url);
    };
    server.events.on('request:start', listener);

    renderPage();
    await screen.findByText('Admin');

    // Must fail if a per-role detail request is issued.
    expect(urls.length).toBe(1);
    server.events.removeListener('request:start', listener);
  });

  it('marks system roles distinctly', async () => {
    renderPage();
    await screen.findByText('Admin');
    // Must fail if no visual distinction exists.
    expect(screen.getByText('System role')).toBeInTheDocument();
    expect(screen.getByText('Developer')).toBeInTheDocument();
  });
});
