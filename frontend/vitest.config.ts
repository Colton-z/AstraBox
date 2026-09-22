import { defineConfig } from 'vitest/config';
import { resolve } from 'path';

// Unit-test baseline config — intentionally minimal and decoupled from the Vite
// build (vite.config.ts). The default environment stays 'node': most of the
// suite is PURE LOGIC (utils/format.ts, api.ts's error-shaping chain, the
// src/session session-state modules) and needs no DOM. `jsdom` and
// `@testing-library/react` are installed as devDependencies for the component
// shell tests under src/session/components/*.test.tsx — each of those opts into the
// DOM per-file via a `// @vitest-environment jsdom` docblock at the top of the
// file (see vitest's test-environment docs) rather than flipping this default,
// so the pure-logic tests keep running on the faster, DOM-free environment.
export default defineConfig({
  resolve: {
    alias: {
      // Under the shadow (see below) __dirname realpaths back to the shared
      // tree; anchoring the alias at the invoking cwd keeps aliased imports
      // inside the shadow so their bare deps resolve natively too.
      '@': process.env.ASTRABOX_FE_FS_ALLOW
        ? resolve(process.cwd(), 'src')
        : resolve(__dirname, 'src'),
    },
    // Dual-environment dev support: when the working tree is reached through
    // a machine-local shadow directory (deps on a native disk, sources
    // symlinked back to the shared tree), realpathing would move importers
    // back onto the shared tree and bare imports would stop resolving to the
    // native node_modules. Keeping symlinked paths intact anchors every
    // resolution inside the shadow. Env-guarded so committed behavior on a
    // plain checkout is untouched, and no machine path lives here.
    ...(process.env.ASTRABOX_FE_FS_ALLOW ? { preserveSymlinks: true } : {}),
  },
  ...(process.env.ASTRABOX_FE_FS_ALLOW
    ? { server: { fs: { allow: [__dirname, process.env.ASTRABOX_FE_FS_ALLOW] } } }
    : {}),
  test: {
    environment: 'node',
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    globals: false,
  },
});
