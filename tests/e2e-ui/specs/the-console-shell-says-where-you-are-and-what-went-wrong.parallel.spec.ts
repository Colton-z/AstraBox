/**
 * E2E: navigation, language, filters, and failure cards expose verifiable state.
 *
 * The journey needs only a signed-in cookie. It checks the rail in both
 * directions, verifies that locale changes reach page copy, and reads date and
 * log filters from their outgoing requests because those filters execute on
 * the server. Listing routes provide deterministic records without intercepting
 * the control requests under test. A page-error listener covers render-time
 * failures in the date picker and shared UI components.
 */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { appPath } from '../fixtures/env';

const SESSIONS_LISTING = '**/api/v1/admin/sessions/all*';
const ERRORS_LISTING = '**/api/v1/admin/errors*';
const LOGS_LISTING = '**/api/v1/admin/logs*';

/** The deployment's own words, which the failure card has to quote back. */
const ERRORS_FAILURE = 'synthetic error-records listing failure';
const LOG_FILE = 'astrabox-e2e-console-shell.log';
const LOG_KEYWORD = 'astrabox-e2e-marker';

/** The success envelope `api.ts` unwraps: anything but `code: "OK"` is a refusal. */
function envelope(data: unknown): string {
  return JSON.stringify({ code: 'OK', data });
}

/**
 * One conversation, so `/manage/sessions` offers its narrowing controls.
 *
 * The toolbar is earned, not fixed: `SessionsListPage` withdraws the agent
 * picker and the date window on a deployment whose collection is empty, so a
 * date filter driven against the live listing is a control that is present on
 * a busy box and absent on a fresh one. The window under test is read off the
 * REQUEST, which this answer cannot influence.
 */
const STUB_SESSION_PAGE = {
  items: [
    {
      session_id: '00000000-0000-4000-8000-0000000e2e00',
      user_id: 'e2e-console-shell',
      display_name: 'e2e console shell',
      template_name: 'e2e console shell',
      state: 'READY',
      sandbox_id: 'e2e-console-shell-box',
      created_at: new Date().toISOString(),
      duration_seconds: 12,
      has_local_runtime: false,
    },
  ],
  pagination: { page: 1, page_size: 50, total_items: 1, total_pages: 1 },
};

/** One file with one line, so the toolbar renders whatever this host logs to. */
const STUB_LOG_WINDOW = {
  available_files: [LOG_FILE],
  current_file: LOG_FILE,
  lines: ['2026-01-01 00:00:00 INFO astrabox e2e console shell log line'],
  count: 1,
  log_path: `/var/log/astrabox/${LOG_FILE}`,
  machine_id: 'e2e-console-shell',
};

test('navigation, language, filters and failure cards each change something the reader can check', async ({
  page,
}) => {
  const uncaught: string[] = [];
  // Attached before the first navigation: every crash this guards happens
  // during a render, and a listener added afterwards misses the one that
  // mattered.
  page.on('pageerror', (error) => uncaught.push(error.message));

  // The rail is held by the kit's own slot rather than by the `navigation`
  // landmark's name, because that name is `shell:primary_navigation` — one of
  // the strings the language section changes. A locator keyed to it would stop
  // resolving halfway through the very assertion it is there to make.
  const rail = page.locator('[data-slot="sidebar"]');
  const homeRow = rail.getByRole('link', { name: 'Home', exact: true });
  const consoleRow = rail.getByRole('link', { name: 'Console', exact: true });

  // ── 1. The rail says which surface you are on, and keeps saying it ────────
  await page.goto(appPath('/'));
  // `/` is a redirect to `/agents` (App.tsx), so the application surface's
  // address is the one to assert against.
  await expect(page).toHaveURL((url) => url.pathname === appPath('/agents'));
  await expect(homeRow, 'the application surface must be the marked row').toHaveAttribute('data-active', '');
  await expect(consoleRow).not.toHaveAttribute('data-active');

  await consoleRow.click();
  await expect(page).toHaveURL((url) => url.pathname === appPath('/manage/agents'));
  await expect(consoleRow, 'the console row must take the mark on arrival').toHaveAttribute('data-active', '');
  await expect(homeRow).not.toHaveAttribute('data-active');

  // Both directions. A rail that paints the mark once and never moves it again
  // passes a one-way check and is wrong on every trip back.
  await homeRow.click();
  await expect(page).toHaveURL((url) => url.pathname === appPath('/agents'));
  await expect(homeRow, 'the mark must come back with the reader').toHaveAttribute('data-active', '');
  await expect(consoleRow).not.toHaveAttribute('data-active');

  await consoleRow.click();
  await expect(page).toHaveURL((url) => url.pathname === appPath('/manage/agents'));

  // ── 2. The language switch reaches the interface, not just its own state ──
  // The console's switcher lives inside the settings menu, so the menu is part
  // of the control: a switcher nobody can open is a switcher nobody can use.
  // Escape after each choice, then reopen — whether the panel survives a
  // selection is the kit's business and not something to assume in either
  // direction.
  await page.getByRole('button', { name: 'Settings' }).click();
  // A native `<select>`, so the role is combobox and the interaction is a
  // selection. Its accessible name is `shell:language`, which is the same
  // string the segmented control carried before it.
  await page.getByRole('combobox', { name: 'Language' }).selectOption('zh');
  await page.keyboard.press('Escape');
  // `manage:nav.sessions`. The control showing its own new value would prove
  // only that the select selects; the rail is where the change has to land.
  await expect(
    rail.getByRole('link', { name: '会话', exact: true }),
    'choosing ZH must change the interface, not only the control',
  ).toBeVisible();

  // Back to English through a switcher that is now labelled in Chinese itself:
  // the trigger and the control's own name are two more of the strings that
  // moved.
  await page.getByRole('button', { name: '设置' }).click();
  await page.getByRole('combobox', { name: '语言' }).selectOption('en');
  await page.keyboard.press('Escape');
  await expect(
    rail.getByRole('link', { name: 'Sessions', exact: true }),
    'English must undo it — the switch persists to astrabox-lang, and every section below reads English',
  ).toBeVisible();

  // ── 3. The date window reaches the query ─────────────────────────────────
  const sessionQueries: string[] = [];
  await page.route(SESSIONS_LISTING, async (route) => {
    sessionQueries.push(new URL(route.request().url()).search);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: envelope(STUB_SESSION_PAGE),
    });
  });
  await page.goto(appPath('/manage/sessions'));

  // The trigger is held structurally because its LABEL is what the filter
  // changes — the handle cannot also be the assertion.
  const dateTrigger = page.locator('[data-slot="popover-trigger"]');
  await expect(dateTrigger, 'an unset window reads as every date').toHaveText('All dates');

  const beforePreset = sessionQueries.length;
  await dateTrigger.click();
  await page.getByRole('button', { name: 'Last 7 days' }).click();

  await expect
    .poll(() => sessionQueries.length, {
      timeout: 15_000,
      message: 'choosing a window must re-ask the server',
    })
    .toBeGreaterThan(beforePreset);
  const windowed = sessionQueries.slice(beforePreset).map((search) => new URLSearchParams(search));
  const since = windowed.map((params) => params.get('since')).find((value) => value);
  expect(
    since,
    'the window has to be in the query — a page that filters in the browser answers a different question',
  ).toBeTruthy();
  // The preset spans seven days. The bounds allow for any timezone and hour of
  // the day while rejecting a different window.
  const sinceAgeDays = (Date.now() - Date.parse(String(since))) / 86_400_000;
  expect(sinceAgeDays, `"Last 7 days" sent since=${since}`).toBeGreaterThan(5);
  expect(sinceAgeDays, `"Last 7 days" sent since=${since}`).toBeLessThan(8);

  // The popup is a step, not a destination: it closes itself once the window is
  // chosen, and the trigger becomes the reader's record of what is in force.
  await expect(page.getByRole('button', { name: 'Last 7 days' })).toBeHidden();
  await expect(dateTrigger).not.toHaveText('All dates');
  await expect(dateTrigger, 'the trigger must name the window it applied').toHaveText(
    /[A-Za-z]{3,}\s+\d{1,2}\s*–\s*[A-Za-z]{3,}\s+\d{1,2}/,
  );

  // A filter that cannot be undone is a trap, and the way out has to reach the
  // query as well — a Clear that only repaints the trigger leaves the list
  // narrowed with nothing on screen saying so.
  const beforeClear = sessionQueries.length;
  await page.getByRole('button', { name: 'Clear date range' }).click();
  await expect
    .poll(() => sessionQueries.length, {
      timeout: 15_000,
      message: 'clearing the window must re-ask the server',
    })
    .toBeGreaterThan(beforeClear);
  // On the LAST request rather than on every one after the click: the preset
  // above can settle into more than one request, and a late arrival from it
  // would otherwise read as the window surviving the Clear.
  await expect
    .poll(() => new URLSearchParams(sessionQueries[sessionQueries.length - 1]).has('since'), {
      timeout: 15_000,
      message: 'after Clear the query must carry no window at all',
    })
    .toBe(false);
  await expect(dateTrigger).toHaveText('All dates');
  await page.unroute(SESSIONS_LISTING);

  // ── 4. A listing that failed says so, and offers the way back ────────────
  // `/manage/errors` rather than the MCP-clients page: this is `ConsoleErrorState`,
  // the console's full-card failure — `data-slot="error-state"`, `role="alert"`,
  // Retry — and `McpTokensPage` renders the inline `ConsoleError` note instead,
  // which carries neither the slot nor a retry. The slot is not decoration: the
  // visual-grammar audit waits on it to know a record page has settled.
  let errorListingCalls = 0;
  await page.route(ERRORS_LISTING, async (route) => {
    errorListingCalls += 1;
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ code: 'ADMIN_ERRORS_E2E_FAILURE', message: ERRORS_FAILURE }),
    });
  });
  await page.goto(appPath('/manage/errors'));

  const failureCard = page.locator('[data-slot="error-state"][role="alert"]');
  await expect(failureCard, 'a listing that failed must not read as a listing with nothing in it').toBeVisible();
  await expect(
    failureCard,
    'the card quotes the deployment, so the reader can paste it into a ticket',
  ).toContainText(ERRORS_FAILURE);

  const beforeRetry = errorListingCalls;
  expect(beforeRetry, 'the faulted listing must have been asked once').toBeGreaterThan(0);
  await failureCard.getByRole('button', { name: 'Retry' }).click();
  await expect
    .poll(() => errorListingCalls, {
      timeout: 15_000,
      message: 'Retry has to re-ask; a button that only re-renders the card is a dead end',
    })
    .toBeGreaterThan(beforeRetry);
  await page.unroute(ERRORS_LISTING);

  // ── 5. The log filters are a query, not a sieve over what arrived ────────
  const logQueries: string[] = [];
  await page.route(LOGS_LISTING, async (route) => {
    logQueries.push(new URL(route.request().url()).search);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: envelope(STUB_LOG_WINDOW),
    });
  });
  await page.goto(appPath('/manage/logs'));

  const keyword = page.getByPlaceholder('Filter by keyword');
  const fileSelect = page.getByRole('combobox').filter({ hasText: LOG_FILE });
  const levelSelect = page.getByRole('combobox').filter({ hasText: 'All levels' });
  const linesSelect = page.getByRole('combobox').filter({ hasText: 'Last 200 lines' });
  const apply = page.getByRole('button', { name: 'Apply' });
  // All five, because the toolbar is all-or-nothing: it is gated on the server
  // reporting a file, and a spec that drove three of them would read a missing
  // control as a passing one.
  await expect(keyword).toBeVisible();
  await expect(fileSelect).toBeVisible();
  await expect(levelSelect).toBeVisible();
  await expect(linesSelect).toBeVisible();
  await expect(apply).toBeVisible();

  await keyword.fill(LOG_KEYWORD);
  await levelSelect.click();
  await page.getByRole('option', { name: 'ERROR', exact: true }).click();
  // 500 rather than the 200 already selected: choosing the value that is
  // already in force asks the select for nothing, and the request would carry
  // `lines=200` with the control disconnected.
  await linesSelect.click();
  await page.getByRole('option', { name: 'Last 500 lines' }).click();

  const beforeApply = logQueries.length;
  await apply.click();
  await expect
    .poll(() => logQueries.length, { timeout: 15_000, message: 'Apply must issue the narrowed request' })
    .toBeGreaterThan(beforeApply);
  const applied = new URLSearchParams(logQueries[logQueries.length - 1]);
  expect(applied.get('level'), 'the level must be narrowed by the server').toBe('ERROR');
  expect(applied.get('keyword'), 'the keyword must be narrowed by the server').toBe(LOG_KEYWORD);
  expect(applied.get('lines'), 'the line count must be the one that was chosen').toBe('500');

  // Off the stub, and the live deployment answers for itself. Both states are
  // legitimate — a host that logs to a file has the toolbar, a host that logs
  // to stderr has the empty state — so this asserts that exactly ONE of them is
  // on screen. Asserting either alone would pass silently on the other host.
  await page.unroute(LOGS_LISTING);
  await page.reload();
  const noLogFiles = page.getByText('No log files to read', { exact: true });
  await expect
    .poll(async () => (await apply.isVisible()) || (await noLogFiles.isVisible()), {
      timeout: 30_000,
      message: 'the live logs page must settle into one of its two real states',
    })
    .toBe(true);
  const liveArms = [await apply.isVisible(), await noLogFiles.isVisible()].filter(Boolean);
  expect(
    liveArms.length,
    'a toolbar above "no log files", or neither, is the failure §5 names by name',
  ).toBe(1);

  // ── 6. A conversation that does not exist gets a frame, not a blank page ──
  await page.goto(appPath(`/sessions/${randomUUID()}`));
  await expect(
    page.getByRole('heading', { name: 'Session', exact: true }),
    'the heading names the record TYPE — a session route gets no topbar, so nothing else names the surface',
  ).toBeVisible();
  await expect(page.getByText("This session doesn't exist or you don't have access.")).toBeVisible();
  // role=link, specifically: this is an `<a>` wearing the button's shape through
  // `buttonVariants`, because the kit's `Button` would impose button semantics
  // on it. `SessionBootstrapStates.test.tsx` pins the same thing one layer down.
  const backHome = page.getByRole('link', { name: 'Back to home' });
  await expect(backHome).toBeVisible();
  await backHome.click();
  await expect(page).toHaveURL((url) => url.pathname === appPath('/agents'));

  expect(
    uncaught,
    `uncaught exception while driving the console shell:\n${uncaught.join('\n')}`,
  ).toEqual([]);
});
