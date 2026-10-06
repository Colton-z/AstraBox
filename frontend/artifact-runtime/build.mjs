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
  const external =
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
  const globals = {
    react: '__artifactReact',
    'react/jsx-runtime': '__artifactJSX',
    'react/jsx-dev-runtime': '__artifactJSXDev',
    'react-dom': '__artifactReactDOM',
    'react-dom/client': '__artifactReactDOMClient',
  };
  await build({
    root,
    configFile: resolve(root, 'vite.artifact-runtime.config.ts'),
    build: {
      lib: {
        entry: resolve(root, 'artifact-runtime', entry),
        name: `AstraBox_${name.replaceAll('-', '_')}`,
        cssFileName: 'theme',
        fileName: () => `${name}.js`,
      },
      rollupOptions: {
        external,
        output: { globals, inlineDynamicImports: true },
      },
    },
  });
}
