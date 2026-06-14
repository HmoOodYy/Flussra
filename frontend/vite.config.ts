import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig(({ mode }) => ({
  plugins: [react()],

  server: {
    host: '0.0.0.0',
    // allowedHosts: true lets any hostname reach the dev server.
    // This is intentionally permissive so Cloudflare Quick Tunnel URLs
    // (which change every session) work without manual config edits.
    // Only applies to the dev server — production builds are static files
    // served by whatever host you choose.
    allowedHosts: true,

    // Dev-only proxy: forwards /api/* requests from the browser to the
    // backend running on localhost:8000.  The browser (including the
    // Claude preview sandbox) only ever talks to the Vite dev server on
    // the same origin — Vite relays the request server-side to 127.0.0.1,
    // which it can always reach because it runs on the host machine.
    // This has zero effect on auth logic, tokens, permissions, or route
    // guards.  It does not exist in production builds.
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },

  // In development the API base URL becomes the relative path /api so
  // that requests flow through the proxy above.  In production mode
  // (npm run build) this define is absent, so the real VITE_API_BASE_URL
  // from .env.local is baked into the bundle as-is — no change to the
  // production deploy behaviour.
  define: mode === 'development'
    ? { 'import.meta.env.VITE_API_BASE_URL': '"/api"' }
    : {},
}))
