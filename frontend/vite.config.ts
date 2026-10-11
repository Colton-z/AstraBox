import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { resolve } from 'path';

// The prebuilt console is served below whatever path a deployment publishes it
// under (ASTRABOX_PUBLIC_PATH_PREFIX), which no build can know. Every built file
// therefore names the others relative to itself (`base: './'`), except the HTML
// shell: it is answered at every console route, where a relative reference
// would resolve against the route. The shell names its entry, preloads and
// stylesheet, and the `astrabox-base-path` meta element in index.html, through
// this token, which the AstraBox server replaces with the prefix on every
// request (astrabox/api/frontend_cache.py) and refuses to serve without.
const BASE_PATH_TOKEN = '__ASTRABOX_BASE_PATH__';

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    {
      // The dev server answers the console at the root of its own origin.
      name: 'astrabox-dev-base-path',
      apply: 'serve',
      transformIndexHtml: (html) => html.replaceAll(BASE_PATH_TOKEN, ''),
    },
  ],
  resolve: {
    alias: [
      // Exact match only: the shim itself imports `ansi-to-react/lib/index.js`,
      // and a prefix alias would rewrite that too and point the module at
      // itself. See src/lib/ansiToReact.ts for what the shim is for.
      {
        find: /^ansi-to-react$/,
        replacement: resolve(__dirname, 'src/lib/ansiToReact.ts'),
      },
      { find: '@', replacement: resolve(__dirname, 'src') },
    ],
  },
  base: './',
  experimental: {
    renderBuiltUrl: (filename, { hostType }) =>
      hostType === 'html' ? `${BASE_PATH_TOKEN}/${filename}` : undefined,
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        // The entry's filename is a contract, not cosmetics: the release guard
        // in src/frontendRelease.ts identifies the running release by this URL
        // and reloads a resident tab when it changes. Rollup's default names the
        // chunk after the HTML file, so leaving it unpinned ties a runtime
        // invariant to a bundler default that no test here would notice moving.
        entryFileNames: 'assets/main-[hash].js',
      },
    },
  },
  server: {
    // Bind the dev server to loopback by default (local-only posture). Override
    // for container/WSL dev where the browser reaches Vite on another interface:
    // ASTRABOX_DEV_FRONTEND_HOST=0.0.0.0 (matches scripts/dev.sh's ASTRABOX_DEV_* knobs).
    host: process.env.ASTRABOX_DEV_FRONTEND_HOST || '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: process.env.ASTRABOX_DEV_PROXY_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: true,
        ws: true
      }
    }
  }
});
