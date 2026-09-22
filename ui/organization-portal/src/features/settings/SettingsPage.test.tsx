import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it, vi } from 'vitest';
import { MockAuthProvider } from '../../auth/MockAuthProvider';
import { server } from '../../mocks/server';
import { SettingsPage } from './SettingsPage';

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MockAuthProvider>
        <SettingsPage />
      </MockAuthProvider>
    </QueryClientProvider>,
  );
}

describe('SettingsPage', () => {
  it('renders the organization profile from a single request', async () => {
    const requestSpy = vi.fn();
    server.events.on('request:start', ({ request }) => {
      if (new URL(request.url).pathname === '/v1/organization/summary') requestSpy();
    });
    renderPage();
    // Must fail if a second/duplicate request is issued or any field is missing.
    expect(await screen.findByText('Acme Corporation')).toBeInTheDocument();
    expect(screen.getByText('acme')).toBeInTheDocument();
    expect(screen.getByText('org_001')).toBeInTheDocument();
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(requestSpy).toHaveBeenCalledTimes(1);
  });

  it('has no edit controls', async () => {
    renderPage();
    await screen.findByText('Acme Corporation');
    // Must fail if any such control is present.
    expect(screen.queryByRole('button', { name: /edit/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /save/i })).toBeNull();
  });

  it('copies the org id to the clipboard', async () => {
    const writeText = vi.fn();
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    renderPage();
    await screen.findByText('org_001');

    await userEvent.click(screen.getByRole('button', { name: 'Copy' }));

    // Must fail if the clipboard is never written to, or the wrong value is written.
    expect(writeText).toHaveBeenCalledWith('org_001');
  });

  it('shows plan entitlement with no change-plan action', async () => {
    renderPage();
    await screen.findByText('Acme Corporation');
    // Must fail if the limits are missing or a change-plan control is present.
    expect(screen.getByText('Pro')).toBeInTheDocument();
    expect(screen.getByText('100K')).toBeInTheDocument();
    expect(screen.getByText('600')).toBeInTheDocument();
    expect(screen.getByText('1M')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /change plan/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /upgrade/i })).toBeNull();
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
