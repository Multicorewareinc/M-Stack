import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// The viewer's API is the SDK's own `python -m multistack.state.viewer` (default port 8765). In
// dev, Vite proxies /api/ to it; in a build, that same Python process serves dist/ and /api/ from
// one origin, so there is nothing to configure. Trailing slash: Vite matches proxy keys as string
// prefixes, so '/api' would also catch a future SPA route like "/api-docs".
const API_TARGET = process.env.STATE_VIEWER_API ?? 'http://127.0.0.1:8765';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5176,
    proxy: { '/api/': { target: API_TARGET, changeOrigin: true } },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    include: ['src/**/*.test.{ts,tsx}'],
  },
});
