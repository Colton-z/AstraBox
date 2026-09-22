/**
 * The clipped-indicator checker distinguishes three controlled geometries.
 *
 * One container clips an animated halo at its border box. The other two keep
 * the halo visible through an overflow margin or available space. The fixture
 * supplies fixed geometry so animation timing cannot turn a clipped halo into
 * a clean result.
 */
import { test, expect } from '@playwright/test';

import { findClippedDecorations } from '../fixtures/clippedDecorations';

const DOT = `
  @keyframes astra-pulse {
    0%   { box-shadow: 0 0 0 0 rgba(92,91,255,.55); }
    80%  { box-shadow: 0 0 0 7px rgba(92,91,255,0); }
    100% { box-shadow: 0 0 0 0 rgba(92,91,255,0); }
  }
  .astra-dot { display:inline-block; width:6px; height:6px; border-radius:999px; background:#5c5bff; }
  .astra-dot--live { animation: astra-pulse 1.6s linear infinite; }
  .clip-content { overflow: clip; overflow-clip-margin: 8px; }
  .row { display:flex; align-items:center; gap:8px; }
`;

const row = (id: string) =>
  `<div class="row"><span id="${id}" class="astra-dot astra-dot--live"></span><span>Generating</span></div>`;

test('the checker reads a clipped halo, and passes the two that are not', async ({ page }) => {
  await page.setContent(`
    <style>${DOT}</style>
    <div id="clips" style="width:300px;overflow:hidden">${row('bitten')}</div>
    <div id="margin" class="clip-content" style="width:300px">${row('whole')}</div>
    <div id="roomy" style="width:300px;overflow:hidden;padding:12px">${row('padded')}</div>
  `);
  // The animation has to be running before its keyframes can be read off it.
  await page.waitForFunction(() => document.getElementById('bitten')?.getAnimations().length === 1);

  const found = await findClippedDecorations(page);

  // Every side that has no room, not just the one a reader happens to notice:
  // the reported defect was "the left of the dot is cut", and the same halo is
  // cut top and bottom by the same container.
  expect(found.filter((f) => f.includes('left reach 7px into 0px'))).toHaveLength(1);
  expect(found.filter((f) => f.includes('top reach 7px into 6px'))).toHaveLength(1);
  expect(found.filter((f) => f.includes('bottom reach 7px into 6px'))).toHaveLength(1);
  // And nothing from the two that are correct. Both dots are identical to the
  // bitten one; only their container differs, so a checker that reported them
  // would be reading the decoration and not the geometry.
  expect(found, 'a clip margin wide enough for the halo is not a finding').toEqual(
    found.filter((f) => f.includes('clipped by div.')),
  );
  expect(found).toHaveLength(3);
});

/**
 * The two shapes the checker walked past, fed to it.
 *
 * It read only `hidden` and `clip`, and a scroll container clips its content
 * exactly as `hidden` does — so a horizontally scrolling step switcher whose
 * first button sat flush against it, and a scrolling answer list whose rows ran
 * under the scrollbar, were both invisible to the check while a reader
 * described them as "covered on both sides". Green meant it could not read the
 * rule, which is the one thing a checker added after the fact must be made to
 * prove it is not.
 */
test('a scroll container clips too, and its scrollbar covers what it sits on', async ({ page }) => {
  await page.setContent(`
    <style>${DOT}</style>
    <div id="xscroll" style="width:300px;overflow-x:auto">${row('flush')}</div>
    <div id="yscroll" style="width:300px;height:40px;overflow-y:scroll">
      <div class="row"><span>Generating</span><span id="under" class="astra-dot astra-dot--live"
        style="margin-left:auto"></span></div>
      <div style="height:200px"></div>
    </div>
    <div id="padded" style="width:300px;overflow-x:auto;padding:12px">${row('roomy')}</div>
  `);
  await page.waitForFunction(() => document.getElementById('flush')?.getAnimations().length === 1);

  const found = await findClippedDecorations(page);

  // The switcher shape: flush against a scroller that clips on both axes,
  // because `overflow-x: auto` resolves the other axis to `auto` as well.
  expect(found.filter((f) => f.includes('left reach 7px into 0px'))).toHaveLength(1);
  // The answer-row shape: room on the right ends at the scrollbar, not at the
  // border box, so a decoration the scrollbar covers is a finding.
  expect(found.filter((f) => f.includes('right reach 7px into 0px'))).toHaveLength(1);
  // Exactly those four sides and no more. Padding is room, so the third
  // scroller contributes nothing — without this count the check could report
  // every scroll container on every page and still look correct above.
  // (Findings dedupe by message, and all three dots carry the same class, so
  // the two shapes share their top/bottom lines.)
  expect(found).toHaveLength(4);
});
