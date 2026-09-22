import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import * as React from 'react';
import { ApiError } from '../api/client';

export type ErrorView = 'access-denied' | 'session-expired' | null;

const ErrorViewContext = React.createContext<ErrorView>(null);

export function useGlobalErrorView(): ErrorView {
  return React.useContext(ErrorViewContext);
}

/**
 * Any FORBIDDEN/UNAUTHORIZED from any query or mutation takes the whole page
 * over via Shell (§74, §230, §304) — wired once here, not per-page. Every
 * other error code is left to each page's own existing inline handling.
 */
function classify(error: unknown, setErrorView: (view: ErrorView) => void) {
  if (!(error instanceof ApiError)) return;
  if (error.code === 'FORBIDDEN') setErrorView('access-denied');
  else if (error.code === 'UNAUTHORIZED') setErrorView('session-expired');
}

export function QueryProvider({ children }: { children: React.ReactNode }) {
  const [errorView, setErrorView] = React.useState<ErrorView>(null);

  const [queryClient] = React.useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { retry: 1, refetchOnWindowFocus: false },
        },
        queryCache: new QueryCache({ onError: (error) => classify(error, setErrorView) }),
        mutationCache: new MutationCache({ onError: (error) => classify(error, setErrorView) }),
      }),
  );

  return (
    <QueryClientProvider client={queryClient}>
      <ErrorViewContext.Provider value={errorView}>{children}</ErrorViewContext.Provider>
    </QueryClientProvider>
  );
}
