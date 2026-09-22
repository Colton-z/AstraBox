/**
 * E2E: the list pages at a real number of rows.
 *
 * The console table paginates nothing and virtualises nothing — it renders
 * every row and scrolls its own body. Two hundred rows exercise whether the
 * header stays fixed, the body owns vertical scroll, and the page avoids
 * horizontal growth.
 *
 * The rows are multiplied, not invented: the real response is fetched first and
 * its own first row is cloned with distinct identities. A hand-written payload
 * tests the parser against an assumed shape rather than the producer's, which
 * is how a detector ends up green while production is dead.
 */
import { test, expect, type Route } from '@playwright/test';

import { appPath } from '../fixtures/env';

const ROWS = 200;

test('an agent list of 200 keeps its shell, its scroll and its width', async ({ page }) => {
  // take the real envelope once, before anything is intercepted
  const real = await page.request.get(appPath('/api/v1/agents'));
  expect(real.ok(), 'the list endpoint must answer before it can be multiplied').toBeTruthy();
  const body = await real.json();
  // The envelope is the producer's, read rather than assumed: this API answers
  // `{code, message, data: [...]}`. A spec that guesses `items` reports an
  // empty list on a deployment that has one, and passes while doing it.
  const items: unknown[] = Array.isArray(body) ? body : (body.data ?? []);
  expect(items.length, `need one real row to clone; envelope keys: ${Object.keys(body)}`)
    .toBeGreaterThan(0);

  const seed = items[0] as Record<string, unknown>;
  const many = Array.from({ length: ROWS }, (_, i) => ({
    ...seed,
    agent_id: `scale-${String(i).padStart(3, '0')}`,
    name: `Scale agent ${i}`,
  }));
  const payload = Array.isArray(body) ? many : { ...body, data: many };

  // Everything below measures the page's behaviour on 200 rows, so the page
  // has to have RECEIVED 200 rows. Left unchecked, a mock that never matched
  // reads exactly like a console that paginates: the row count comes back
  // small and the assertion blames the product. Both the fulfils and every
  // agents-ish URL the page actually asked for are recorded, because the two
  // failures need different fixes and only the URLs tell them apart.
  let fulfilled = 0;
  const asked: string[] = [];
  page.on('request', (request) => {
    if (request.url().includes('/agents')) asked.push(`${request.method()} ${request.url()}`);
  });
  await page.route('**/api/v1/agents', async (route: Route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    fulfilled += 1;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(payload),
    });
  });

  await page.goto(appPath('/manage/agents'));

  // Network idle can precede the data request. Wait for the route handler to
  // match before attributing a small row count to the console.
  await expect
    .poll(() => fulfilled, {
      message:
        'the 200-row response was never served, so nothing below is measuring it. '
        + `Agents requests the page made: ${asked.join(' | ') || '(none)'}`,
      timeout: 30_000,
    })
    .toBeGreaterThan(0);

  const rows = page.locator('[data-testid="console-table"] .console-row, [data-slot="table-row"]');
  // Matching the request does not prove its rows have rendered. Wait for the
  // DOM count before measuring the list's scroll and width.
  await expect
    .poll(() => rows.count(), {
      message: `every row should render — nothing paginates here (mock served ${fulfilled}x)`,
      timeout: 30_000,
    })
    .toBeGreaterThan(50);

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
