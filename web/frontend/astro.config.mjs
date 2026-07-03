import { defineConfig } from 'astro/config';
import react from '@astrojs/react';

// Dev server proxies API traffic to the FastAPI backend on :4000 so the
// dashboard talks to the same paths in dev and production (where FastAPI
// serves the built dist/ directly).
export default defineConfig({
  integrations: [react()],
  server: { port: 4321 },
  vite: {
    server: {
      proxy: {
        '/admin': 'http://127.0.0.1:4000',
        '/v1': 'http://127.0.0.1:4000',
        '/health': 'http://127.0.0.1:4000',
      },
    },
  },
});
