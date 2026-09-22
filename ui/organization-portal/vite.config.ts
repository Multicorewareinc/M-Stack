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
    port: 5174,
    proxy: {
      '/v1': {
        target: 'http://localhost:9220',
        changeOrigin: true,
      },
      // The auth + api-keys routers (modules/auth, modules/api_keys) live at /api/..., not under
      // /v1 — the one route group that isn't behind the shared service-key check (see their
      // router.py header comments). Trailing slash matters: Vite matches proxy keys as plain
      // string prefixes, and '/api' (no slash) would ALSO match the frontend route /api-keys —
      // meaning a page reload there gets sent to the backend instead of the SPA, 404ing.
      '/api/': {
        target: 'http://localhost:9220',
        changeOrigin: true,
      },
      '/mg/': {
        target: 'http://localhost:9230',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/mg/, ''),
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
