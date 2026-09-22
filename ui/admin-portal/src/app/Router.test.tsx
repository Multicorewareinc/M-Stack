import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { MockAuthProvider } from '../auth/MockAuthProvider';
import { AppRoutes } from './routes';

describe('Router', () => {
  it('renders not-found for unknown route', () => {
    const queryClient = new QueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <MockAuthProvider>
          <MemoryRouter initialEntries={['/this-route-does-not-exist']}>
            <AppRoutes />
          </MemoryRouter>
        </MockAuthProvider>
      </QueryClientProvider>,
    );
    // Must fail if an unmatched route renders nothing or throws.
    expect(screen.getByText('Page not found')).toBeInTheDocument();
  });
});
