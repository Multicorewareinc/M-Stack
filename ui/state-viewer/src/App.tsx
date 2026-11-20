import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import * as React from 'react';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { RefreshProvider } from './app/RefreshContext';
import { Shell } from './app/Shell';
import { ActivityPage } from './features/activity/ActivityPage';
import { DeploymentsPage } from './features/deployments/DeploymentsPage';

export function App() {
  const [queryClient] = React.useState(
    () => new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: true } } }),
  );

  return (
    <QueryClientProvider client={queryClient}>
      <RefreshProvider>
        <BrowserRouter>
          <Routes>
            <Route element={<Shell />}>
              <Route path="/" element={<DeploymentsPage />} />
              <Route path="/activity" element={<ActivityPage />} />
              <Route path="*" element={<DeploymentsPage />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </RefreshProvider>
    </QueryClientProvider>
  );
}
