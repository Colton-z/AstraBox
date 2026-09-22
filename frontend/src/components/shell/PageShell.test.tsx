// @vitest-environment jsdom
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { PageShell } from './PageShell';

afterEach(cleanup);

/**
 * jsdom has no layout, so these assert the nesting and the class contract that
 * decide it. The pixel consequences are the layout gate's job
 * (e2e/specs/layout.shell.spec.ts).
 */
describe('PageShell', () => {
  const measure = (c: HTMLElement) => c.querySelector('[data-slot="page-measure"]')!;
  const scroll = (c: HTMLElement) => c.querySelector('[data-slot="page-scroll"]')!;

  it('keeps every child on one column, not just the ones below the header', () => {
    const { container } = render(
      <PageShell>
        <h1 data-testid="head">Agents</h1>
        <div data-testid="body">rows</div>
      </PageShell>,
    );
    const column = measure(container);
    // Capping only the inner scroller gives the heading and content different
    // left edges, so both must share this measured column.
    expect(column.contains(container.querySelector('[data-testid="head"]'))).toBe(true);
    expect(column.contains(container.querySelector('[data-testid="body"]'))).toBe(true);
  });

  it('caps the column inside the scroller, never the scroller itself', () => {
    const { container } = render(<PageShell>x</PageShell>);
    expect(scroll(container).contains(measure(container))).toBe(true);
    // A max-width on the scroller would centre the scrollbar with the text,
    // giving the page chrome and content different left edges.
    expect(scroll(container).className).not.toMatch(/max-w-/);
  });

  it('has exactly one scroll owner', () => {
    const { container } = render(<PageShell><div>x</div></PageShell>);
    expect(container.querySelectorAll('[data-slot="page-scroll"]')).toHaveLength(1);
  });

  it('selects the measure the caller asked for', () => {
    const { container: wide } = render(<PageShell>x</PageShell>);
    expect(measure(wide).className).toMatch(/\bmax-w-measure\b/);
    expect(measure(wide).className).not.toMatch(/max-w-measure-narrow/);

    cleanup();
    const { container: narrow } = render(<PageShell measure="narrow">x</PageShell>);
    expect(measure(narrow).className).toMatch(/max-w-measure-narrow/);
  });

  it('lets a short page fill the well', () => {
    const { container } = render(<PageShell>x</PageShell>);
    // Without min-h-full an empty list leaves the well's background exposed
    // below it — 350-434px of it at 1440x900.
    expect(measure(container).className).toMatch(/\bmin-h-full\b/);
    expect(container.querySelector('[data-slot="page-shell"]')!.className).toMatch(/\bh-full\b/);
  });

  it('applies the caller className to the column so gap acts on content', () => {
    const { container } = render(<PageShell className="gap-4">x</PageShell>);
    expect(measure(container).className).toMatch(/\bgap-4\b/);
    expect(scroll(container).className).not.toMatch(/\bgap-4\b/);
  });
});


/**
 * A detail panel never covers content the reader still needs
 * (docs/frontend-design.md §3). As an overlay it would clip the list's columns
 * and the page description behind it, and landing straight on `?selected=…`
 * would make a half-cut page the first thing an operator sees.
 */
describe('PageShell detail column', () => {
  it('lays the detail beside the page, not over it', () => {
    const { container } = render(
      <PageShell detail={<aside>panel</aside>}>
        <p>list</p>
      </PageShell>,
    );
    const detail = container.querySelector('[data-slot="page-detail"]');
    expect(detail).toBeTruthy();
    // In flow, not lifted out of it: an absolutely-positioned panel is what
    // would cover the list.
    expect(detail!.className).not.toMatch(/\bfixed\b|\babsolute\b/);
    expect(container.querySelector('[data-slot="page-scroll"]')).toBeTruthy();
  });

  it('leaves the page alone when nothing is selected', () => {
    const { container } = render(
      <PageShell>
        <p>list</p>
      </PageShell>,
    );
    expect(container.querySelector('[data-slot="page-detail"]')).toBeNull();
  });
});
