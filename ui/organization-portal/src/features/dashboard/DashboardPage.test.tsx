import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it, vi } from 'vitest';
import { MockAuthProvider } from '../../auth/MockAuthProvider';
import { server } from '../../mocks/server';
import { DashboardPage } from './DashboardPage';

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MockAuthProvider>
        <DashboardPage />
      </MockAuthProvider>
    </QueryClientProvider>,
  );
}

describe('DashboardPage', () => {
  it('renders counts and plan entitlement from a single request', async () => {
    const requestSpy = vi.fn();
    server.events.on('request:start', ({ request }) => {
      if (new URL(request.url).pathname === '/v1/organization/summary') requestSpy();
    });
    renderPage();
    // Must fail if a second/duplicate request is issued or any value is missing.
    await screen.findByText('Pro');
    // Users and Roles both seed to a count of 2 in the fixtures.
    expect(screen.getAllByText('2')).toHaveLength(2);
    expect(screen.getByText('Pro')).toBeInTheDocument();
    expect(screen.getByText('100K')).toBeInTheDocument();
    expect(screen.getByText('600')).toBeInTheDocument();
    expect(screen.getByText('1M')).toBeInTheDocument();
    expect(requestSpy).toHaveBeenCalledTimes(1);
  });

  it('has no charts or admin-only controls', async () => {
    renderPage();
    await screen.findByText('Pro');
    // Must fail if any such element is present.
    expect(document.querySelector('svg[class*="chart"]')).toBeNull();
    expect(document.querySelector('canvas')).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('shows an inline error when the summary request fails', async () => {
    server.use(
      http.get('/v1/organization/summary', () => HttpResponse.json({ error: { message: 'Internal error', type: 'internal_error' } }, { status: 500 })),
    );
    renderPage();
    // Must fail if the page crashes or renders nothing.
    expect(await screen.findByText(/internal error/i)).toBeInTheDocument();
  });
});
