/**
 * E2E: the number on the rail is the number on the page.
 *
 * Three sections carry a quiet count in the sidebar. The open list publishes
 * the total from the response that rendered its rows into the shared count
 * cache; an inactive list gets one small independent count request. The open
 * page must therefore never say "Agents 4" above a list holding five.
 *
 * The badge is decorative by design and tolerates its own request failing by
 * rendering nothing, which is right — a missing count is honest and a wrong one
 * is not. This asserts only the second part: when a number IS shown, the page
 * behind it agrees.
 */
import { test, expect, type Page } from '@playwright/test';

import { appPath } from '../fixtures/env';

type Badge = { href: string; label: string };

async function badges(page: Page): Promise<Badge[]> {
  await page.goto(appPath('/manage/agents'));
  await page.waitForLoadState('networkidle');
  // The list and the independent inactive counts settle after the shell.
  await page.locator('[data-slot="sidebar-menu-badge"]').first()
    .waitFor({ state: 'attached', timeout: 8000 })
    .catch(() => undefined);

  return page.evaluate(() =>
    [...document.querySelectorAll<HTMLElement>('[data-slot="sidebar-menu-badge"]')]
      .map((badge) => {
        const item = badge.closest('li');
        const link = item?.querySelector<HTMLAnchorElement>('a[href^="/manage/"]');
        return {
          href: link?.getAttribute('href') || '',
          label: (link?.textContent || '').trim(),
        };
      })
      .filter((b) => b.href),
  );
}

test('every count on the rail matches the list it points at', async ({ page }) => {
  const shown = await badges(page);

  // A run that found no badges would pass every assertion below without making
  // a single comparison. Say so instead: the console has three of them.
  expect(shown.length, 'the rail must be showing counts for this to check anything')
    .toBeGreaterThan(0);

  for (const badge of shown) {
    await page.goto(appPath(badge.href));
    // `networkidle` can fire before React's effect starts the list request. All
    // three counted lists disable this control until that request has settled.
    await expect(page.getByRole('button', { name: 'Refresh' }),
      `${badge.label} (${badge.href}): the list must finish loading`).toBeEnabled();
    // Each collection page states its full total beside the h1. Sessions is
    // paginated, so counting its 50 visible rows would compare a page size with
    // the deployment-wide total rendered in the rail.
    const pageMeta = page.getByRole('heading', { level: 1 })
      .locator('xpath=following-sibling::span[1]');
    await expect(pageMeta).toHaveText(/\d/);
    const totalText = await pageMeta.innerText();
    const totalMatch = totalText.match(/\d+/);
    expect(totalMatch, `the page summary must state a collection total: ${totalText}`).not.toBeNull();
    const actual = Number(totalMatch![0]);
    // Another administrator may write between two routes. Read the badge on
    // the page it describes, after that page has published the same total,
    // instead of comparing the page with a snapshot from a previous route.
    const currentBadge = page.locator(
      `nav li:has(a[href="${badge.href}"]) [data-slot="sidebar-menu-badge"]`,
    );
    await expect(
      currentBadge,
      `${badge.label} (${badge.href}): the rail must agree with the page total ${actual}`,
    ).toHaveText(String(actual));
  }
});
