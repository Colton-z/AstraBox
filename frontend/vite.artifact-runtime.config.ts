import { defineConfig } from 'vite';
import { resolve } from 'node:path';
export default defineConfig({
  define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  build: {
    outDir: 'dist/artifact-runtime',
    emptyOutDir: false,
    lib: {
      entry: resolve(import.meta.dirname, 'artifact-runtime/bridge.entry.ts'),
      name: 'AstraBoxArtifactBridge',
      formats: ['iife'],
      fileName: () => 'bridge.js',
    },
    rollupOptions: {
      output: { inlineDynamicImports: true, assetFileNames: '[name][extname]' },
    },
  },
});
