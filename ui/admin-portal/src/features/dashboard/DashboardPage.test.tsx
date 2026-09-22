import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { server } from '../../mocks/server';
import { DashboardPage } from './DashboardPage';

function OrganizationsStub() {
  return <div>Organizations page stub</div>;
}
function OrgDetailStub() {
  return <div>Organization detail stub</div>;
}

function renderDashboard() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/organizations" element={<OrganizationsStub />} />
          <Route path="/organizations/:id" element={<OrgDetailStub />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('DashboardPage', () => {
  it('loads summary counts with a single request', async () => {
    const urls: string[] = [];
    const listener = ({ request }: { request: Request }) => {
      if (new URL(request.url).pathname === '/v1/platform-summary') urls.push(request.url);
    };
    server.events.on('request:start', listener);

    renderDashboard();
    await screen.findByText('Organizations');

    // Must fail if a per-organization request is issued for the summary itself.
    expect(urls.length).toBe(1);
    server.events.removeListener('request:start', listener);
  });

  it('recent organizations render and navigate to detail', async () => {
    renderDashboard();
    const row = await screen.findByText('Acme Corporation');
    expect(screen.getAllByText('Active').length).toBeGreaterThan(0);

    await userEvent.click(row);
    // Must fail if the row does not navigate or renders the wrong fields.
    expect(await screen.findByText('Organization detail stub')).toBeInTheDocument();
  });

  it('renders no chart or decorative visualization', async () => {
    const { container } = renderDashboard();
    await screen.findByText('Acme Corporation');
    // Must fail if a chart-like element is introduced.
    expect(container.querySelectorAll('svg[class*="recharts"], canvas, [data-chart]')).toHaveLength(0);
  });

  it('links to Organizations rather than replicating its controls', async () => {
    renderDashboard();
    await screen.findByText('Acme Corporation');
    // Must fail if the dashboard renders its own search/filter/row-action controls.
    expect(screen.queryByLabelText(/search organizations/i)).toBeNull();
    expect(screen.queryByRole('button', { name: 'Row actions' })).toBeNull();

    await userEvent.click(screen.getByRole('button', { name: 'View all organizations' }));
    expect(await screen.findByText('Organizations page stub')).toBeInTheDocument();
  });
});
