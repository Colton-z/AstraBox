import { transform } from 'sucrase';
import type { PageWindow } from './bridge';

type Registry = Record<string, any>;
const page = window as PageWindow & {
  __artifactModules: Registry;
  __artifactSource: string;
  __artifactBase: string;
  __artifactRelease: string;
};
const registry = (page.__artifactModules ??= {});
const modules = [
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
];
const ui = [
  'button',
  'card',
  'input',
  'label',
  'textarea',
  'select',
  'checkbox',
  'switch',
  'tabs',
  'badge',
  'alert',
  'separator',
  'progress',
  'slider',
  'dialog',
];
const available = [...modules, ...ui.map((name) => `@/components/ui/${name}`)];
const loading = new Map<string, Promise<void>>();
const load = (name: string): Promise<void> => {
  if (registry[name]) return Promise.resolve();
  const bundle = name.startsWith('@/components/ui/')
    ? 'ui'
    : name.startsWith('react-dom')
      ? 'react-dom'
      : name.startsWith('react/')
        ? 'react'
        : name;
  if (!modules.includes(bundle) && bundle !== 'ui' && bundle !== 'tailwind')
    return Promise.reject(
      new Error(
        `Unknown import "${name}". Available modules: ${available.join(', ')}`,
      ),
    );
  if (!loading.has(bundle))
    loading.set(
      bundle,
      new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = `${page.__artifactBase}/lib-${bundle}.js?release=${encodeURIComponent(page.__artifactRelease)}`;
        let executionError: unknown;
        const captureError = (event: ErrorEvent) => {
          if (event.filename === script.src)
            executionError ??= event.error ?? new Error(event.message);
        };
        page.addEventListener('error', captureError);
        const cleanup = () => page.removeEventListener('error', captureError);
        script.onload = () => {
          cleanup();
          if (executionError) return reject(executionError);
          const expected = bundle === 'react'
            ? ['react', 'react/jsx-runtime', 'react/jsx-dev-runtime']
            : bundle === 'react-dom'
              ? ['react-dom', 'react-dom/client']
              : bundle === 'ui'
                ? ui.map((component) => `@/components/ui/${component}`)
                : bundle === 'tailwind' ? [] : [bundle];
          const missing = expected.filter((module) => !registry[module]);
          if (missing.length)
            reject(new Error(`Artifact library "${bundle}" did not initialize: missing ${missing.join(', ')}.`));
          else resolve();
        };
        script.onerror = () => {
          cleanup();
          reject(new Error(`Could not load artifact library "${bundle}".`));
        };
        document.head.append(script);
      }),
    );
  return loading.get(bundle)!;
};
function report(error: unknown) {
  page.__artifactReportError?.(error);
  const panel = document.createElement('pre');
  panel.setAttribute('role', 'alert');
  panel.className = 'artifact-render-error';
  panel.textContent = error instanceof Error ? error.message : String(error);
  document.getElementById('root')!.replaceChildren(panel);
}
void (async () => {
  await page.__artifactReady;
  const code = transform(page.__artifactSource, {
    transforms: ['jsx', 'typescript', 'imports'],
    jsxRuntime: 'classic',
    production: true,
  }).code;
  await load('react');
  await load('react-dom');
  const imports = [
    ...code.matchAll(/\brequire\(\s*['"]([^'"]+)['"]\s*\)/g),
  ].map((match) => match[1]);
  await Promise.all([...new Set(imports)].map(load));
  await load('tailwind');
  const exports: Registry = {};
  const module = { exports };
  const require = (name: string) => {
    if (!registry[name])
      throw new Error(
        `Unknown import "${name}". Available modules: ${available.join(', ')}`,
      );
    return registry[name];
  };
  new Function('require', 'module', 'exports', 'React', code)(
    require,
    module,
    exports,
    registry.react,
  );
  const Component =
    module.exports.default ??
    Object.values(module.exports)
      .filter((value) => typeof value === 'function')
      .at(-1);
  if (!Component)
    throw new Error(
      'Export a React component as the default export or a named export.',
    );
  const React = registry.react;
  class Boundary extends React.Component {
    state = { error: null };
    static getDerivedStateFromError(error: unknown) {
      return { error };
    }
    componentDidCatch(error: unknown) {
      page.__artifactReportError?.(error);
    }
    render() {
      return this.state.error
        ? React.createElement(
            'pre',
            { role: 'alert', className: 'artifact-render-error' },
            String(this.state.error),
          )
        : (this as any).props.children;
    }
  }
  registry['react-dom/client']
    .createRoot(document.getElementById('root'), {
      onUncaughtError: report,
      onCaughtError: (error: unknown) => page.__artifactReportError?.(error),
    })
    .render(
      React.createElement(Boundary, null, React.createElement(Component)),
    );
})().catch(report);
