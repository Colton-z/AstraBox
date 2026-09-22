import { describe, expect, it } from 'vitest';

import { cn } from './utils';

/**
 * `cn()` must not eat a utility it was handed.
 *
 * tailwind-merge deduplicates by class group, and it derives the group from the
 * class name. This project's font sizes (`--text-10/11/13/15` → `text-10`…`text-15`)
 * are not on its built-in t-shirt scale, so without the extension in `utils.ts`
 * they fall into the colour group and are dropped whenever a colour sits beside
 * them in the same call — the element then renders at the inherited 16px
 * instead of the size written at the call site.
 *
 * The failure is silent and invisible in the browser: the class is gone before
 * it reaches the DOM, so nothing on the rendered page carries evidence of it.
 * The browser-side audit
 * (tests/e2e-ui/specs/visual-grammar.parallel.spec.ts) can only see the other
 * failure mode, a class that is present and loses in the cascade, so this half
 * has to be checked here.
 */
describe('cn', () => {
  const SIZES = ['text-10', 'text-11', 'text-13', 'text-15'];

  it.each(SIZES)('keeps %s when a text colour is merged after it', (size) => {
    expect(cn(size, 'text-muted-foreground')).toContain(size);
  });

  it.each(SIZES)('keeps %s when a colour comes first', (size) => {
    expect(cn('text-foreground', size)).toContain(size);
  });

  it('still lets one font size replace another', () => {
    expect(cn('text-13', 'text-sm')).toBe('text-sm');
    expect(cn('text-sm', 'text-13')).toBe('text-13');
  });

  it('still lets one colour replace another', () => {
    expect(cn('text-foreground', 'text-muted-foreground')).toBe('text-muted-foreground');
  });

  it('keeps a size and a colour together — they are different properties', () => {
    const out = cn('text-13', 'text-crimson').split(' ');
    expect(out).toContain('text-13');
    expect(out).toContain('text-crimson');
  });
});
