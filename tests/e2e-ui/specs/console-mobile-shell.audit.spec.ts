/**
 * E2E: the console on a phone.
 *
 * The sidebar already carries a mobile branch — the vendored component swaps
 * its rail for a Sheet below the breakpoint. This verifies that the application
 * shell around it reaches that behavior. A rail that stays in the
 * layout at 390px does not throw; it just pushes the page sideways, and a
 * horizontal scrollbar on a phone reads as "this site is not for you".
 */
import { test, expect } from '@playwright/test';

import { appPath } from '../fixtures/env';

const PHONE = { width: 390, height: 844 };

test.use({ viewport: PHONE });

test('the sidebar leaves the layout and returns as a sheet', async ({ page }) => {
  await page.goto(appPath('/manage/agents'));
  await page.waitForLoadState('networkidle');

  // 1. nothing may scroll sideways. Measured on the document element, because
  //    a child that overflows still widens this.
  const overflow = await page.evaluate(() => {
    const d = document.documentElement;
    return { scrollW: d.scrollWidth, clientW: d.clientWidth };
  });
  expect(
    overflow.scrollW,
    `the page scrolls sideways on a ${PHONE.width}px screen (${overflow.scrollW} > ${overflow.clientW})`,
  ).toBeLessThanOrEqual(overflow.clientW + 1);

  // 2. the rail must not be holding layout width open behind the content
  const railWidth = await page.evaluate(() => {
    const rail = document.querySelector('[data-slot="sidebar"]');
    if (!rail) return 0;
    return Math.round(rail.getBoundingClientRect().width);
  });
  expect(railWidth, 'the desktop rail must not occupy width on a phone').toBeLessThan(100);

  // 3. the trigger has to be there, or the navigation is simply gone
  const trigger = page.locator('[data-sidebar="trigger"], [data-slot="sidebar-trigger"]').first();
  await expect(trigger, 'a phone needs a control that brings the navigation back').toBeVisible();

  // 4. and it has to work: the sheet opens and carries the console sections
  await trigger.click();
  const sheet = page.locator('[data-slot="sheet-content"], [role="dialog"]').first();
  await expect(sheet).toBeVisible();
  const links = sheet.locator('a[href^="/manage/"]');
  // Retrying for the same reason the at-scale spec now does: a sheet that is
  // visible has not necessarily finished rendering its list, and a count read
  // once measures whatever moment it lands in.
  await expect
    .poll(() => links.count(), {
      message: 'the mobile sheet must carry the sections',
      timeout: 10_000,
    })
    .toBeGreaterThan(3);

  // 5. Escape closes it — a sheet a phone cannot dismiss is a trap
  await page.keyboard.press('Escape');
  await expect(sheet).toBeHidden();
});
