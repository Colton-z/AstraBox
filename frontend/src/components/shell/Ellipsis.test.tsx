// @vitest-environment jsdom
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { Ellipsis } from './Ellipsis';

afterEach(cleanup);

/**
 * jsdom reports every element as 0x0, so scrollWidth/clientWidth have to be
 * stubbed to say whether the text is clipped. Clipped and unclipped are the two
 * inputs the rule under test branches on — "disclose only when actually
 * clipped" — so each case sets the stub to the state it is asserting about.
 */
function setClipping(el: HTMLElement, { clipped }: { clipped: boolean }) {
  Object.defineProperty(el, 'scrollWidth', { value: clipped ? 200 : 100, configurable: true });
  Object.defineProperty(el, 'clientWidth', { value: 100, configurable: true });
  Object.defineProperty(el, 'scrollHeight', { value: clipped ? 200 : 100, configurable: true });
  Object.defineProperty(el, 'clientHeight', { value: 100, configurable: true });
}

describe('Ellipsis', () => {
  const node = (c: HTMLElement) => c.querySelector('[data-slot="ellipsis"]') as HTMLElement;

  it('discloses the full text once the text is clipped', () => {
    const { container } = render(<Ellipsis>a very long agent name</Ellipsis>);
    const el = node(container);
    setClipping(el, { clipped: true });
    fireEvent.mouseEnter(el);
    expect(el.getAttribute('title')).toBe('a very long agent name');
  });

  it('stays silent when the text fits', () => {
    // The reason disclosure is measured rather than unconditional: a table full
    // of short cells must not grow a tooltip on every one of them.
    const { container } = render(<Ellipsis>short</Ellipsis>);
    const el = node(container);
    setClipping(el, { clipped: false });
    fireEvent.mouseEnter(el);
    expect(el.hasAttribute('title')).toBe(false);
  });

  it('drops a stale title when the box grows back', () => {
    const { container } = render(<Ellipsis>a very long agent name</Ellipsis>);
    const el = node(container);
    setClipping(el, { clipped: true });
    fireEvent.mouseEnter(el);
    expect(el.hasAttribute('title')).toBe(true);
    setClipping(el, { clipped: false });
    fireEvent.mouseEnter(el);
    expect(el.hasAttribute('title')).toBe(false);
  });

  it('prefers an explicit title over the text', () => {
    const { container } = render(<Ellipsis title="/very/long/absolute/path.py">path.py</Ellipsis>);
    const el = node(container);
    setClipping(el, { clipped: true });
    fireEvent.mouseEnter(el);
    expect(el.getAttribute('title')).toBe('/very/long/absolute/path.py');
  });

  it('discloses nothing when the caller opted out with null', () => {
    // The opt-out exists for places that already disclose some other way — the
    // sidebar row, whose button carries a tooltip of the same text.
    const { container } = render(
      <Ellipsis title={null}>
        <em>rich</em>
      </Ellipsis>,
    );
    const el = node(container);
    setClipping(el, { clipped: true });
    fireEvent.mouseEnter(el);
    expect(el.hasAttribute('title')).toBe(false);
  });

  it('can shrink inside a flex or grid track', () => {
    // Without min-w-0 the box refuses to shrink below its content and pushes
    // the overflow onto an ancestor, which clips it with no ellipsis at all.
    const { container } = render(<Ellipsis>x</Ellipsis>);
    expect(node(container).className).toMatch(/\bmin-w-0\b/);
  });

  it('clamps to several lines when asked', () => {
    const { container } = render(<Ellipsis lines={2}>long prose</Ellipsis>);
    const el = node(container);
    expect(el.className).not.toMatch(/\btruncate\b/);
    // React's style prop spells it WebkitLineClamp; the DOM reads back webkitLineClamp.
    expect(el.style.webkitLineClamp).toBe('2');
  });

  it('rejects non-string children with no disclosure', () => {
    // @ts-expect-error non-string children must pass an explicit title (or null)
    const bad = <Ellipsis><em>rich</em></Ellipsis>;
    expect(bad).toBeTruthy();
  });
});
