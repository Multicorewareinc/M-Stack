import { render, screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { MockAuthProvider } from '../auth/MockAuthProvider';
import { server } from '../mocks/server';
import { QueryProvider } from './QueryProvider';
import { AppRoutes } from './routes';

function renderApp(path = '/') {
  return render(
    <QueryProvider>
      <MockAuthProvider>
        <MemoryRouter initialEntries={[path]}>
          <AppRoutes />
        </MemoryRouter>
      </MockAuthProvider>
    </QueryProvider>,
  );
}

describe('Org Shell', () => {
  it('renders org navigation', () => {
    renderApp();
    // Must fail if a nav item is missing.
    for (const label of ['Overview', 'Users', 'Roles', 'Permissions', 'Settings']) {
      expect(screen.getByRole('link', { name: label })).toBeInTheDocument();
    }
    expect(screen.getByText('Acme Corporation')).toBeInTheDocument();
  });

  it('marks active nav item accessibly', () => {
    renderApp('/roles');
    expect(screen.getByRole('link', { name: 'Roles' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: 'Overview' })).not.toHaveAttribute('aria-current');
  });

  it('renders AccessDenied when any query returns FORBIDDEN', async () => {
    server.use(
      http.get('/v1/organization/summary', () => HttpResponse.json({ error: { message: 'Forbidden', type: 'forbidden' } }, { status: 403 })),
    );
    renderApp();
    // Must fail if the routed page still renders, or a page-local error message renders instead.
    // Generous timeout: the real QueryProvider retries once before settling into error.
    expect(await screen.findByText('Access denied', {}, { timeout: 5000 })).toBeInTheDocument();
  }, 10000);

  it('renders SessionExpired when any query returns UNAUTHORIZED', async () => {
    server.use(
      http.get('/v1/organization/summary', () => HttpResponse.json({ error: { message: 'Unauthorized', type: 'unauthorized' } }, { status: 401 })),
    );
    renderApp();
    // Must fail if the routed page still renders, or a page-local error message renders instead.
    expect(await screen.findByText('Session expired', {}, { timeout: 5000 })).toBeInTheDocument();
  }, 10000);

  it("leaves non-identity errors to the page's own handling", async () => {
    server.use(
      http.get('/v1/organization/summary', () => HttpResponse.json({ error: { message: 'Boom', type: 'internal_error' } }, { status: 500 })),
    );
    renderApp();
    // Must fail if the page is replaced by AccessDenied/SessionExpired for a non-identity error.
    expect(await screen.findByText('Boom', {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByText('Access denied')).toBeNull();
    expect(screen.queryByText('Session expired')).toBeNull();
  }, 10000);
});
