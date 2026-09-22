import react from '@vitejs/plugin-react';
import { defineConfig, loadEnv } from 'vite';

/**
 * The agentic service guards every /v1 route with a shared X-API-Key
 * (agentic/backend/src/dependencies.py). That key must never reach the browser, so
 * the dev proxy attaches it server-side from AGENTIC_API_KEY in the shell
 * or a local .env -- the SPA calls /agentic/... with no credential at all.
 *
 * Proxying also sidesteps CORS: the agentic app registers no CORS
 * middleware, so a direct cross-origin fetch from :5175 to :8000 would be
 * blocked by the browser before the request ever arrived.
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const target = env.AGENTIC_URL || 'http://localhost:8000';
  const apiKey = env.AGENTIC_API_KEY || '';

  return {
    plugins: [react()],
    server: {
      port: 5175,
      proxy: {
        '/agentic': {
          target,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/agentic/, ''),
          configure: (proxy) => {
            proxy.on('proxyReq', (proxyReq) => {
              if (apiKey) proxyReq.setHeader('X-API-Key', apiKey);
            });
          },
        },
      },
    },
  };
});
