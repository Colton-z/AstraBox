// These are the supported module shapes, plus the one that must fail loudly.
import { describe, expect, it } from 'vitest';

import { resolveComponent } from './ansiToReact';

const component = () => null;

describe('resolving the ansi component through whatever interop delivered it', () => {
  it('takes a bare function, which is what the dev server hands back', () => {
    expect(resolveComponent(component)).toBe(component);
  });

  it('unwraps one level of esModule interop', () => {
    expect(resolveComponent({ __esModule: true, default: component })).toBe(component);
  });

  it('unwraps the build\'s namespace, where the component sits one level deeper', () => {
    // What rolldown produces: `__toESM(exports, nodeMode)` defines `default` as
    // the exports object itself, so the component is at `.default.default`.
    const exports = { __esModule: true, default: component };
    expect(resolveComponent({ __esModule: true, default: exports })).toBe(component);
  });

  it('throws rather than returning something unrenderable', () => {
    // A throw at module load names the module and the interop mismatch before an
    // invalid component reaches the session tree.
    expect(() => resolveComponent({ default: { nothing: true } })).toThrow(/interop changed/);
    expect(() => resolveComponent(null)).toThrow(/interop changed/);
  });
});
