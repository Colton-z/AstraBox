import { build } from 'vite';
import { resolve } from 'node:path';
const root = resolve(import.meta.dirname, '..');
const libraries = [
  'react',
  'react-dom',
  'lucide-react',
  'recharts',
  'lodash',
  'd3',
  'papaparse',
  'mathjs',
  'three',
  'xlsx',
  'ui',
  'tailwind',
];
for (const name of [
  'bridge',
  'react',
  'mermaid',
  ...libraries.map((name) => `lib-${name}`),
]) {
  const library = name.startsWith('lib-');
  const entry = library
    ? `libs/${name.slice(4)}.${name === 'lib-ui' ? 'tsx' : 'ts'}`
    : name === 'bridge'
      ? 'bridge.entry.ts'
      : `${name}.ts`;
  const provided =
    library && name !== 'lib-react' && name !== 'lib-tailwind'
      ? [
          'react',
          'react/jsx-runtime',
          'react/jsx-dev-runtime',
          ...(name === 'lib-react-dom'
            ? []
            : ['react-dom', 'react-dom/client']),
        ]
      : [];
  await build({
    root,
    configFile: resolve(root, 'vite.artifact-runtime.config.ts'),
    resolve: {
      alias: provided.map((id) => ({
        find: new RegExp(`^${id}$`),
        replacement: resolve(root, 'artifact-runtime/globals', `${id.replaceAll('/', '-')}.cjs`),
      })),
    },
    build: {
      lib: {
        entry: resolve(root, 'artifact-runtime', entry),
        name: `AstraBox_${name.replaceAll('-', '_')}`,
        cssFileName: 'theme',
        fileName: () => `${name}.js`,
      },
      rollupOptions: {
        output: { inlineDynamicImports: true },
      },
    },
  });
}
