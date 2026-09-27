/**
 * E2E: the Agent list at a real number of rows.
 *
 * The console table virtualises nothing — it renders every row it holds and
 * scrolls its own body. The Agent list is paged by the server, 50 at a time,
 * with a "Load more" control under the table. Two hundred and fifty Agents
 * exercise both halves: the header and the rail state the whole list's size
 * from the first page, "Load more" reaches every row, and with every row on
 * screen the header stays fixed, the body owns vertical scroll, and the page
 * avoids horizontal growth.
 *
 * The rows are multiplied, not invented: the real first page is fetched and
 * its own envelope and first row are cloned with distinct identities. A
 * hand-written payload tests the parser against an assumed shape rather than
 * the producer's, which is how a detector ends up green while production is
 * dead. The cursor the mock hands out is its own; the console treats every
 * cursor as opaque, which is the contract this relies on.
 */
import { test, expect, type Route } from '@playwright/test';

import { appPath } from '../fixtures/env';

const ROWS = 250;
const PAGE = 50;

test('an agent list of 250 is counted whole, reached by Load more, and keeps its shell', async ({ page }) => {
  // take the real page envelope once, before anything is intercepted
  const real = await page.request.get(appPath('/api/v1/agents?page=1&limit=1'));
  expect(real.ok(), 'the list endpoint must answer before it can be multiplied').toBeTruthy();
  const body = await real.json();
  // The envelope is the producer's, read rather than assumed: this API answers
  // `{code, message, data: {agents: [...], has_more, next_cursor, total}}`.
  const items: unknown[] = body?.data?.agents ?? [];
  expect(items.length, `need one real row to clone; data keys: ${Object.keys(body?.data ?? {})}`)
    .toBeGreaterThan(0);

  const seed = items[0] as Record<string, unknown>;
  const many = Array.from({ length: ROWS }, (_, i) => ({
    ...seed,
    agent_id: `scale-${String(i).padStart(3, '0')}`,
    name: `Scale agent ${String(i).padStart(3, '0')}`,
    enabled: true,
  }));

  // Everything below measures the page's behaviour on 250 rows, so the page
  // has to have RECEIVED them. Left unchecked, a mock that never matched reads
  // exactly like a console that cannot page: the row count comes back small
  // and the assertion blames the product. Both the fulfils and every
  // agents-ish URL the page actually asked for are recorded, because the two
  // failures need different fixes and only the URLs tell them apart.
  let fulfilled = 0;
  const asked: string[] = [];
  page.on('request', (request) => {
    if (request.url().includes('/agents')) asked.push(`${request.method()} ${request.url()}`);
  });
  await page.route('**/api/v1/agents?*', async (route: Route) => {
    const url = new URL(route.request().url());
    if (route.request().method() !== 'GET' || url.searchParams.get('page') !== '1') {
      return route.fallback();
    }
    fulfilled += 1;
    const start = Number(url.searchParams.get('cursor') || '0');
    const end = start + PAGE;
    const data = {
      ...body.data,
      agents: many.slice(start, end),
      has_more: end < ROWS,
      next_cursor: end < ROWS ? String(end) : null,
      ...(start === 0 ? { total: ROWS, enabled: ROWS } : { total: undefined, enabled: undefined }),
    };
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ...body, data }),
    });
  });

  await page.goto(appPath('/manage/agents'));

  // Network idle can precede the data request. Wait for the route handler to
  // match before attributing a small row count to the console.
  await expect
    .poll(() => fulfilled, {
      message:
        'the paged response was never served, so nothing below is measuring it. '
        + `Agents requests the page made: ${asked.join(' | ') || '(none)'}`,
      timeout: 30_000,
    })
    .toBeGreaterThan(0);

  // The whole list's size, from the first page, before any row past it loads.
  await expect(page.getByRole('heading', { level: 1 }).locator('xpath=following-sibling::span[1]'))
    .toContainText(String(ROWS));

  const rows = page.locator('[data-testid="console-table"] .console-row, [data-slot="table-row"]');
  const loadMore = page.getByRole('button', { name: 'Load more' });
  for (let shown = PAGE; shown < ROWS; shown += PAGE) {
    await expect.poll(() => rows.count(), { timeout: 30_000 }).toBeGreaterThanOrEqual(shown);
    await loadMore.click();
  }
  await expect
    .poll(() => rows.count(), {
      message: `every row should be reachable through Load more (mock served ${fulfilled}x)`,
      timeout: 30_000,
    })
    .toBeGreaterThanOrEqual(ROWS);
  await expect(loadMore).toHaveCount(0);
  await expect(page.getByText(`Scale agent ${String(ROWS - 1).padStart(3, '0')}`)).toBeVisible();

  const m = await page.evaluate(() => {
    const doc = document.documentElement;
    // Ask which ancestor of a row actually owns the scroll rather than naming
    // one. `.console-tbody` is the tempting guess and it is wrong — it is
    // `overflow-y: hidden` and never scrolls, so asserting on it fails against
    // a page that is behaving correctly.
    const row = document.querySelector('.console-row');
    let owner: string | null = null;
    for (let el = row?.parentElement; el && el !== doc; el = el.parentElement) {
      const cs = getComputedStyle(el);
      if (/auto|scroll/.test(cs.overflowY) && el.scrollHeight > el.clientHeight + 1) {
        owner = `${el.tagName.toLowerCase()} [${el.clientHeight}→${el.scrollHeight}]`;
        break;
      }
    }
    return {
      docScrollW: doc.scrollWidth,
      docClientW: doc.clientWidth,
      docScrollH: doc.scrollHeight,
      docClientH: doc.clientHeight,
      scrollOwner: owner,
    };
  });

  expect(m.docScrollW, 'a long list must not widen the page').toBeLessThanOrEqual(m.docClientW + 1);
  expect(
    m.scrollOwner,
    'some region inside the shell has to own the scroll; if none does, the list pushed the page taller and took the shell with it',
  ).not.toBeNull();
  expect(
    m.docScrollH,
    `the page itself must not grow with the list (${m.docScrollH} vs viewport ${m.docClientH})`,
  ).toBeLessThanOrEqual(m.docClientH + 2);
});
