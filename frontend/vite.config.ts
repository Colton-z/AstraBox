import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { resolve } from 'path';

export default defineConfig({
  plugins: [react(), tailwindcss()],
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
  base: '/',
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
