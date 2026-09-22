/**
 * The other half of §11's geometry: a decoration an ancestor clips.
 *
 * §11 already says a paint that reaches past the border box must not land on a
 * neighbour. The same reach fails the other way when there is no neighbour to
 * land on: an ancestor clipping at its border box simply does not paint it, and
 * the reader sees an indicator with a side sliced off. The reported shape was
 * the live dot in front of "Generating" — a 6px dot whose halo grows to 7px,
 * sitting against the left edge of a message body that clipped — so the halo
 * appeared bitten off on its left.
 *
 * Measured, not modelled, for the reasons §11 gives and one more: the halo is
 * an ANIMATION, so its reach at the instant a reader looks is whatever the
 * clock says. Computed style at a random frame reports somewhere between 0 and
 * 7px and can report a clean 0 for the exact defect. The keyframes hold the
 * number the design chose, so they are what gets measured.
 */
import type { Page } from '@playwright/test';

/** Elements whose decoration is cut off by an ancestor, as readable lines. */
export async function findClippedDecorations(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    type Side = 'left' | 'right' | 'top' | 'bottom';

    const outward = (boxShadow: string, into: Record<Side, number>) => {
      if (!boxShadow || boxShadow === 'none') return;
      for (const layer of boxShadow.split(/,(?![^(]*\))/)) {
        if (layer.includes('inset')) continue;
        const lengths = [...layer.matchAll(/(-?\d+(?:\.\d+)?)px/g)].map((m) => Number(m[1]));
        if (lengths.length < 2) continue;
        const [offsetX, offsetY, blur = 0, spread = 0] = lengths;
        into.left = Math.max(into.left, blur + spread - offsetX);
        into.right = Math.max(into.right, blur + spread + offsetX);
        into.top = Math.max(into.top, blur + spread - offsetY);
        into.bottom = Math.max(into.bottom, blur + spread + offsetY);
      }
    };

    const found: string[] = [];
    for (const el of document.querySelectorAll('*')) {
      const rect = el.getBoundingClientRect();
      if (rect.width < 1 || rect.height < 1) continue;
      const cs = getComputedStyle(el);
      if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue;

      const reach: Record<Side, number> = { left: 0, right: 0, top: 0, bottom: 0 };
      const outlineReach =
        cs.outlineStyle === 'none'
          ? 0
          : (Number.parseFloat(cs.outlineWidth) || 0) + (Number.parseFloat(cs.outlineOffset) || 0);
      for (const side of ['left', 'right', 'top', 'bottom'] as Side[]) reach[side] = outlineReach;
      outward(cs.boxShadow, reach);
      // What the animation reaches at its peak, not at this frame.
      for (const animation of el.getAnimations()) {
        const effect = animation.effect;
        if (!effect || typeof (effect as KeyframeEffect).getKeyframes !== 'function') continue;
        for (const frame of (effect as KeyframeEffect).getKeyframes()) {
          const shadow = (frame as unknown as { boxShadow?: string }).boxShadow;
          if (typeof shadow === 'string') outward(shadow, reach);
        }
      }
      if (Math.max(reach.left, reach.right, reach.top, reach.bottom) <= 0) continue;

      for (let node = el.parentElement; node; node = node.parentElement) {
        const ancestor = getComputedStyle(node);
        // `auto` and `scroll` clip exactly as `hidden` does. Reading only
        // `hidden`/`clip` walked straight past a horizontally scrolling step
        // switcher whose first and last buttons sat flush against it, and past
        // a scrolling answer list whose rows did the same — both reported as
        // "covered on both sides" by a reader, neither visible to this. And
        // the axis a container does NOT scroll still clips: CSS resolves a
        // `visible` paired with a non-`visible` to `auto`, so `overflow-x-auto`
        // clips vertically too.
        const clips = (value: string) =>
          value === 'hidden' || value === 'clip' || value === 'auto' || value === 'scroll';
        const clipsX = clips(ancestor.overflowX);
        const clipsY = clips(ancestor.overflowY);
        if (!clipsX && !clipsY) continue;
        const margin =
          ancestor.overflowX === 'clip' || ancestor.overflowY === 'clip'
            ? Number.parseFloat(ancestor.overflowClipMargin) || 0
            : 0;
        const raw = node.getBoundingClientRect();
        // A scrollbar covers what it sits on, so the room a decoration has ends
        // where the scrollbar starts, not at the border box. The gutter is
        // whatever the border box has that the client box does not.
        const border = (side: string) =>
          Number.parseFloat(ancestor.getPropertyValue(`border-${side}-width`)) || 0;
        const gutterX = Math.max(0, raw.width - border('left') - border('right') - node.clientWidth);
        const gutterY = Math.max(0, raw.height - border('top') - border('bottom') - node.clientHeight);
        const box = {
          left: raw.left + border('left'),
          right: raw.right - border('right') - gutterX,
          top: raw.top + border('top'),
          bottom: raw.bottom - border('bottom') - gutterY,
        };
        // An element the container has already scrolled or clipped away is not
        // a finding about its decoration.
        const inside =
          rect.left >= box.left && rect.right <= box.right
          && rect.top >= box.top && rect.bottom <= box.bottom;
        if (!inside) break;
        const room: Record<Side, number> = {
          left: rect.left - box.left + margin,
          right: box.right - rect.right + margin,
          top: rect.top - box.top + margin,
          bottom: box.bottom - rect.bottom + margin,
        };
        for (const side of ['left', 'right', 'top', 'bottom'] as Side[]) {
          if (!(side === 'left' || side === 'right' ? clipsX : clipsY)) continue;
          // Half a pixel of slack: subpixel layout, not a design defect.
          if (reach[side] <= room[side] + 0.5) continue;
          found.push(
            `${el.tagName.toLowerCase()}.${(el.className || '').toString().trim().slice(0, 40)}`
            + ` — ${side} reach ${Math.round(reach[side])}px into ${Math.round(room[side])}px`
            + ` (clipped by ${node.tagName.toLowerCase()}.${(node.className || '').toString().trim().slice(0, 40)})`,
          );
        }
        break;
      }
    }
    return [...new Set(found)];
  });
}
