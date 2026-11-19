import { applyAccentColor, applyBackgroundColor, applySurfaceColor, applyTextColor } from '@multistack/ui';
import * as React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './App';
import { shouldUseMockAuth, shouldUseRealAuth } from './auth/env';
import { BRANDING } from './branding';
import './styles/index.css';

// Set at runtime (not via Vite's %VITE_APP_NAME% HTML placeholder) so a fresh clone with no .env
// configured still builds and shows the sane default in index.html's <title> until this runs,
// rather than a build failing or showing a literal unresolved placeholder.
document.title = BRANDING.appName;
// Each is a no-op when unset/invalid (see accentColor.ts/themeSurface.ts) — the shipped default
// palette applies untouched.
applyAccentColor(BRANDING.accentColor);
applyBackgroundColor(BRANDING.backgroundColor);
applySurfaceColor(BRANDING.surfaceColor);
applyTextColor(BRANDING.textColor);

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
