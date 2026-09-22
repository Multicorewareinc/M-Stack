import { existsSync, rmSync } from 'node:fs';
import { resolve } from 'node:path';
import react from '@vitejs/plugin-react';
import { defineConfig, type Plugin } from 'vite';

/**
 * MSW's browser worker script lives in public/ (required for dev), but Vite
 * copies the entire public/ dir into dist/ unconditionally — the JS that
 * registers it is tree-shaken out of the production bundle (verified by
 * src/test/msw-not-in-prod.test.ts), but the static file itself would still
 * ship. Strip it from the production build output only.
 */
function stripMswWorkerFromBuild(): Plugin {
  return {
    name: 'strip-msw-worker-from-build',
    apply: 'build',
    closeBundle() {
      const workerPath = resolve(__dirname, 'dist/mockServiceWorker.js');
      if (existsSync(workerPath)) rmSync(workerPath);
    },
  };
}

export default defineConfig({
  plugins: [react(), stripMswWorkerFromBuild()],
  server: {
    port: 5173,
    proxy: {
      '/v1': {
        target: 'http://localhost:9210',
        changeOrigin: true,
      },
      // The auth router (modules/auth) lives at /api/auth, not under /v1 — it's the one route
      // group that isn't behind the shared service-key check (see its router.py header comment).
      // Trailing slash matters: Vite matches proxy keys as plain string prefixes, so '/api'
      // (no slash) would also match any future frontend route starting with "/api" (e.g.
      // "/api-something"), sending a page reload there to the backend instead of the SPA.
      '/api/': {
        target: 'http://localhost:9210',
        changeOrigin: true,
      },
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    include: ['src/**/*.test.{ts,tsx}'],
    exclude: ['e2e/**'],
  },
});
