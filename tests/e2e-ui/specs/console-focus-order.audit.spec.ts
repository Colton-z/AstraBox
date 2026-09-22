/**
 * E2E: Tab goes forward, Shift+Tab comes back the same way.
 *
 * A widget that manages focus can allow forward traversal while trapping
 * backward traversal. Compare both directions through the same controls.
 *
 * Walk forward N stops, then back N-1 stops to the first recorded control.
 * The reverse sequence must match without prescribing a fixed focus order.
 */
import { test, expect } from '@playwright/test';

import { appPath } from '../fixtures/env';

const STOPS = 12;

/** What is focused, in terms stable enough to compare two visits. */
function focusedId(): string {
  const el = document.activeElement as HTMLElement | null;
  if (!el || el === document.body) return '<body>';
  const bits = [
    el.tagName.toLowerCase(),
    el.getAttribute('data-testid') && `#${el.getAttribute('data-testid')}`,
    el.getAttribute('href') && `[${el.getAttribute('href')}]`,
    el.getAttribute('aria-label') && `(${el.getAttribute('aria-label')})`,
    (el.textContent || '').trim().slice(0, 18),
  ].filter(Boolean);
  return bits.join(' ');
}

test('the focus chain is walkable in both directions', async ({ page }) => {
  await page.goto(appPath('/manage/agents'));
  await page.waitForLoadState('networkidle');
  // Network idle does not establish DOM readiness. Wait for a visible shell
  // link so both walks traverse rendered controls.
  await page.locator('nav a[href^="/manage/"]').first().waitFor({ state: 'visible' });
  await page.locator('body').click({ position: { x: 2, y: 2 } });

  const forward: string[] = [];
  for (let i = 0; i < STOPS; i++) {
    await page.keyboard.press('Tab');
    forward.push(await page.evaluate(focusedId));
  }

  const back: string[] = [];
  for (let i = 0; i < STOPS - 1; i++) {
    await page.keyboard.press('Shift+Tab');
    back.push(await page.evaluate(focusedId));
  }

  // walking back from stop N should pass N-1, N-2, ... 1
  const expected = forward.slice(0, STOPS - 1).reverse();
  const firstDivergence = back.findIndex((v, i) => v !== expected[i]);
  expect(
    back,
    firstDivergence < 0
      ? ''
      : `Shift+Tab diverges at step ${firstDivergence + 1}: went back to ` +
        `"${back[firstDivergence]}" where forward had "${expected[firstDivergence]}".\n` +
        `  forward: ${forward.join(' -> ')}\n` +
        `  back:    ${back.join(' -> ')}`,
  ).toEqual(expected);
});
