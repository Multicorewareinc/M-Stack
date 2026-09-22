import { BrowserRouter } from 'react-router-dom';
import { AppRoutes } from './app/routes';
import { ErrorBoundary } from './app/ErrorBoundary';
import { QueryProvider } from './app/QueryProvider';
import { ToastProvider } from './app/ToastProvider';
import { AuthProvider } from './auth/AuthProvider';
import { MockAuthProvider } from './auth/MockAuthProvider';
import { shouldUseRealAuth } from './auth/env';

export function App() {
  // AuthProvider (real login/refresh flow against modules/auth) in production, or in dev when
  // explicitly opted into via VITE_REAL_AUTH=true. MockAuthProvider otherwise — including the
  // VITE_DEV_API_KEY bridge, a separate, older mechanism (see MockAuthProvider's own getAccessToken).
  const Provider = shouldUseRealAuth() ? AuthProvider : MockAuthProvider;

  return (
    <ErrorBoundary>
      <QueryProvider>
        <Provider>
          <ToastProvider>
            <BrowserRouter>
              <AppRoutes />
            </BrowserRouter>
          </ToastProvider>
        </Provider>
      </QueryProvider>
    </ErrorBoundary>
  );
}
