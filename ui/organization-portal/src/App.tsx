import { ThemeProvider } from '@multistack/ui';
import { BrowserRouter } from 'react-router-dom';
import { AppRoutes } from './app/routes';
import { ErrorBoundary } from './app/ErrorBoundary';
import { QueryProvider } from './app/QueryProvider';
import { ToastProvider } from './app/ToastProvider';
import { AuthProvider } from './auth/AuthProvider';
import { MockAuthProvider } from './auth/MockAuthProvider';
import { shouldUseRealAuth } from './auth/env';
import { AcceptInvitePage, isAcceptInviteRoute } from './features/invitations/AcceptInvitePage';

export function App() {
  // The invitation link is opened by someone with no account yet, so it must render without a session
  // (AuthProvider would otherwise show the sign-in screen for every path).
  if (isAcceptInviteRoute()) {
    return (
      <ThemeProvider>
        <ErrorBoundary>
          <AcceptInvitePage />
        </ErrorBoundary>
      </ThemeProvider>
    );
  }

  // AuthProvider (real login/refresh flow against modules/auth) in production, or in dev when
  // explicitly opted into via VITE_REAL_AUTH=true. MockAuthProvider otherwise — including the
  // VITE_DEV_API_KEY bridge, a separate, older mechanism (see MockAuthProvider's own getAccessToken).
  const Provider = shouldUseRealAuth() ? AuthProvider : MockAuthProvider;

  return (
    // Outermost: even the error screens (ErrorBoundary) should respect the chosen theme.
    <ThemeProvider>
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
    </ThemeProvider>
  );
}
