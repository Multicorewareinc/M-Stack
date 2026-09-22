import * as React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './App';
import { shouldUseMockAuth, shouldUseRealAuth } from './auth/env';
import './styles/index.css';

async function enableMocking() {
  if (!shouldUseMockAuth()) return;
  // A real backend is configured — either the VITE_DEV_API_KEY static-key bridge or the real
  // AuthProvider login/refresh flow (VITE_REAL_AUTH=true) — don't intercept its requests with
  // mock fixtures.
  if (import.meta.env.VITE_DEV_API_KEY || shouldUseRealAuth()) return;
  const { worker } = await import('./mocks/browser');
  return worker.start({ onUnhandledRequest: 'bypass' });
}

if (!shouldUseMockAuth()) {
  // eslint-disable-next-line no-console
  console.warn('No production auth provider wired yet; the auth module is upcoming (Architecture §44).');
}

enableMocking().then(() => {
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
});
