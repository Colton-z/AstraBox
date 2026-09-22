/**
 * `ansi-to-react`, unwrapped once for the whole app.
 *
 * The package is CJS-only and marks itself `__esModule`, so TypeScript's
 * `esModuleInterop` types `import Ansi from 'ansi-to-react'` as the component.
 * The bundler disagrees: rolldown applies Node's CJS semantics, where a default
 * import IS `module.exports`. Its `__toESM(mod, isNodeMode)` helper takes the
 * `isNodeMode` branch and defines `default` as the whole exports object, so the
 * import arrives as `{ __esModule: true, default: Ansi }` — an object where a
 * component is expected. React cannot render that value, and the session has
 * no error boundary above this component.
 *
 * The dev server and production build expose different interop shapes, so this
 * module resolves both before any terminal output is rendered.
 *
 * Aliased in `vite.config.ts` so every importer — including the vendored
 * `ai-elements/terminal.tsx`, which must not be patched — gets the component.
 */
import * as ansiToReact from 'ansi-to-react/lib/index.js';

type AnsiComponent = (props: { children: string }) => React.ReactElement | null;

/**
 * Descend `default` until a function appears.
 *
 * How deep it sits depends on who resolved the module, and there is no fixed
 * answer to hard-code: the dev server hands back the component, while a build
 * wraps the namespace so the component is one `default` further in. Deciding by
 * what the value IS survives the next change of interop; counting levels does
 * not. Bounded, because a cycle here would hang the module rather than fail it.
 */
export function resolveComponent(entry: unknown): AnsiComponent {
  let value = entry;
  for (let depth = 0; depth < 4; depth += 1) {
    if (typeof value === 'function') return value as AnsiComponent;
    if (!value || typeof value !== 'object') break;
    value = (value as { default?: unknown }).default;
  }
  throw new Error(
    'ansi-to-react resolved to a value that cannot be rendered; the module interop changed',
  );
}

export const Ansi = resolveComponent(ansiToReact);

export default Ansi;
