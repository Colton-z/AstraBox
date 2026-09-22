/**
 * E2E: every console surface, checked by axe rather than visual house rules.
 *
 * The visual-grammar spec beside this one asks whether a page obeys the house
 * style. This one asks a different question that those rules cannot answer: does
 * the page work for someone who is not looking at it. The two do not overlap —
 * a page can be perfectly on-grid and still hand a screen reader a button with
 * no name.
 *
 * axe supplies an independently maintained WCAG rule set; hand-written checks
 * would cover only the accessibility failures already anticipated here.
 *
 * Routes come from the console's own sidebar plus one record page per section,
 * not from a list typed here. A list typed here is a list that goes stale the
 * first time a section is added, and it goes stale silently — the run stays
 * green because it never visited the new page.
 *
 * The suite contract runs this deployment-wide census with one worker. Browser
 * contexts isolate browser state, not the shared records this walk discovers;
 * a parallel writer can otherwise delete a row between its list and detail
 * reads, replacing the surface under audit with a not-found page.
 */
import AxeBuilder from '@axe-core/playwright';
import { test, expect, type Page } from '@playwright/test';

import { appPath } from '../fixtures/env';

/** The four tag sets that make up WCAG 2.1 A and AA. */
const WCAG = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'];

type Violation = {
  route: string;
  id: string;
  impact: string;
  help: string;
  nodes: string[];
  /** axe's own account of why each node failed — for `color-contrast` this is
      the foreground, the background and the measured ratio, which is the only
      part a person can act on: the selector is a generated id. */
  why: string[];
};

async function routesFromSidebar(page: Page): Promise<string[]> {
  await page.goto(appPath('/manage/agents'));
  const links = page.locator('[data-slot="sidebar-content"] a[href^="/manage/"]');
  await expect(
    links.first(),
    'the sidebar must render before its console sections are read',
  ).toBeAttached();
  const hrefs = await links.evaluateAll((anchors) => [
    ...new Set(
      anchors.map((anchor) => anchor.getAttribute('href') || ''),
    ),
  ]);
  // A sidebar that produced nothing is a broken page, not a console with no
  // sections. Saying so here keeps a silent zero from reading as a clean sweep.
  expect(hrefs.length, 'the sidebar must name the console sections').toBeGreaterThan(3);
  return hrefs.filter(Boolean);
}

/** Scan the console surface already rendered in `page`. */
async function scan(page: Page, route: string): Promise<Violation[]> {
  expect(new URL(page.url()).pathname, `${route}: the console must reach the requested route`)
    .toBe(route);
  await expect(page.locator('h1'), `${route}: the console surface must render`).toHaveCount(1);
  await expect(
    page.locator('[data-slot="error-state"]'),
    `${route}: the console surface must load before it is measured`,
  ).toHaveCount(0);
  const result = await new AxeBuilder({ page }).withTags(WCAG).analyze();
  console.log(`axe scanned ${route}`);
  return result.violations.map((v) => ({
    route,
    id: v.id,
    impact: v.impact || 'unknown',
    help: v.help,
    nodes: v.nodes.slice(0, 3).map((n) => n.target.join(' ')),
    why: v.nodes
      .slice(0, 3)
      .map((n) => (n.failureSummary || '').replace(/\s+/g, ' ').trim())
      .filter(Boolean),
  }));
}

/**
 * Open the record behind the first actionable row, if the section has one.
 *
 * The list has already finished loading and been scanned. Reusing that page
 * avoids a second list navigation, while `.console-row--click` distinguishes
 * real record links from loading skeletons and inert rows. The route is still
 * discovered by following the row rather than composing an id.
 */
async function openFirstRecord(page: Page, route: string): Promise<string | null> {
  const row = page.locator('.console-row--click').first();
  if (await row.count() === 0) return null;

  const oldHeading = (await page.locator('h1').textContent())?.trim() || '';
  await Promise.all([
    page.waitForURL((url) => url.pathname !== route),
    row.click(),
  ]);
  const path = new URL(page.url()).pathname;
  await expect(
    page.locator('h1'),
    `${path}: opening a record must replace the list surface`,
  ).not.toHaveText(oldHeading);
  await expect(
    page.locator('[data-slot="loading-state"]'),
    `${path}: the record must finish loading before it is measured`,
  ).toHaveCount(0);
  return path;
}

async function openSection(page: Page, route: string): Promise<void> {
  await page.goto(appPath(route));
  await expect(
    page.getByRole('button', { name: 'Refresh', exact: true }),
    `${route}: the section must finish loading before it is measured`,
  ).toBeEnabled();
}

test('every console route passes WCAG 2.1 AA as axe measures it', async ({ page }) => {
  const routes = await routesFromSidebar(page);

  const found: Violation[] = [];
  const visited: string[] = [];
  for (const route of routes) {
    await openSection(page, route);
    found.push(...(await scan(page, route)));
    visited.push(route);
    const record = await openFirstRecord(page, route);
    if (record) {
      found.push(...(await scan(page, record)));
      visited.push(record);
    }
  }
  // Say what was covered. A sweep that quietly visited half the console reads
  // exactly like one that visited all of it.
  console.log(`axe visited ${visited.length} surfaces: ${visited.join(', ')}`);

  // Group by rule so the report says which KIND of thing is wrong, and how
  // widely, rather than listing the same defect once per page it appears on.
  const byRule = new Map<string, Violation[]>();
  for (const v of found) byRule.set(v.id, [...(byRule.get(v.id) || []), v]);
  const report = [...byRule.entries()]
    .sort((a, b) => b[1].length - a[1].length)
    .map(([id, vs]) =>
      `${id} (${vs[0].impact}) × ${vs.length} — ${vs[0].help}\n` +
      `    routes: ${[...new Set(vs.map((v) => v.route))].join(', ')}\n` +
      `    e.g.:   ${vs[0].nodes.join(' | ')}\n` +
      `    why:    ${vs[0].why.join('\n            ') || '(axe gave no summary)'}`)
    .join('\n');

  expect(
    found,
    `${visited.length} surfaces scanned, ${byRule.size} rules violated:\n${report}`,
  ).toHaveLength(0);
});
