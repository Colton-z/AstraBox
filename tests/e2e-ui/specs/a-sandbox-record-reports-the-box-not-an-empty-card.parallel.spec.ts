/**
 * E2E: sandbox record pages report the backend's facts rather than empty cards.
 *
 * The API fixture creates one READY sandbox without a model turn. The console
 * must link the Session named in create metadata, show exactly one security
 * finding, render a report or an actionable alert in every diagnostics scope,
 * and issue a new request for each Refresh. A page-error listener covers
 * render-time failures in the built bundle.
 *
 * Scope: what the record SAYS, asked once per mount. Whether an open record
 * re-asks the control plane when the reader returns to the tab is the other
 * half, and it belongs to
 * `a-sandbox-record-can-be-re-asked-without-a-browser-reload.parallel.spec.ts`
 * — which times its return against `useKeepCurrent`'s 5s throttle
 * (`SandboxDetailPage.tsx:73`). Nothing here dispatches focus or
 * visibilitychange, so a green result makes no claim about that reader.
 */
import { test, expect, type Locator, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { apiPath, appPath } from '../fixtures/env';

// Sessions created here are deleted only when the test passes. A failure keeps
// the box and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

/**
 * The four diagnostics scopes: the wire word the request carries, and the tab
 * label — which is also the word the panel writes into each of its alerts
 * (`manage:sandboxes.scope.*`, read back through `diagnostics_*`).
 */
const SCOPES = [
  { wire: 'summary', tab: 'Summary' },
  { wire: 'inspect', tab: 'Inspect' },
  { wire: 'events', tab: 'Events' },
  { wire: 'logs', tab: 'Logs' },
] as const;

// Every string asserted below is localized — the card headings, the Refresh
// control, the three security sentences, the four tab labels and the alerts
// each panel writes. The console detects language as
// ['localStorage','navigator'], so an unpinned runner locale decides which
// spelling appears, and a spec written to accept two spellings would accept a
// third nobody wrote it against. Pin the navigator locale here and the
// persisted 'astrabox-lang' before the first navigation below.
test.use({ locale: 'en-US' });

/** One console card, addressed by the heading it puts on itself. */
function card(page: Page, heading: string): Locator {
  return page
    .locator('[data-slot="card"]')
    .filter({ has: page.getByRole('heading', { name: heading, exact: true }) });
}

test('the sandbox record pages report what the box answered, and never an empty card', async ({
  page,
  request,
}) => {
  const uncaught: string[] = [];
  // Attached before the first navigation: an interop crash happens during a
  // render, and a listener added afterwards misses the one that mattered.
  page.on('pageerror', (error) => uncaught.push(error.message));

  // Every GET the page issues, in order. Each Refresh below is asserted as a
  // DELTA around its own click rather than as a total: the record page mounts
  // again on the way back from the session it links to, and a total would count
  // that mount as a refresh.
  const gets: string[] = [];
  page.on('request', (req) => {
    if (req.method() === 'GET') gets.push(new URL(req.url()).pathname);
  });
  const hits = (pathname: string) => gets.filter((seen) => seen === pathname).length;

  // ── setup: one conversation, zero model turns ─────────────────────────────
  const api = new AstraApi(request);
  const agent = await api.createColdTestAgent(
    `__e2e_sandbox_record_${new Date().toISOString().replace(/[:.]/g, '-')}`,
  );
  agentId = String(agent.agent_id || '');
  expect(agentId, 'conversation-tenancy Agent created').not.toEqual('');
  expect(
    String(agent.sandbox_id || '').trim(),
    'conversation-tenancy Agent must not own a shared sandbox',
  ).toEqual('');
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  const ready = await api.waitForSessionReady(sessionId);
  const sandboxId = String(ready.sandbox_id || '');
  expect(sandboxId, 'a READY session names the box it claimed').not.toEqual('');

  // ── 1. the listing, and the row that opens the target box ─────────────────
  // Pin the console language before the FIRST navigation (see test.use above):
  // the persisted 'astrabox-lang' outranks navigator in the detection order.
  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* localStorage unavailable — the en-US navigator locale still applies */
    }
  });
  await page.goto(appPath('/manage/sandboxes'));

  // `manage:sandboxes.meta` — the backend's own `total_items`. The header says
  // it precisely because a tally taken from the loaded page would read the same
  // on a deployment running one box and one running four thousand.
  const meta = page.getByText(/^\d+ sandboxes$/);
  await expect(meta, 'the listing header must report the backend inventory count').toBeVisible();
  expect(
    Number.parseInt(await meta.innerText(), 10),
    `the inventory count must include the target box ${sandboxId}`,
  ).toBeGreaterThanOrEqual(1);

  // The search box and the state select are deliberately NOT driven: search
  // renders above twelve rows and the select needs two distinct states on the
  // page, so on a quiet host a spec that drove them would be green because the
  // control was absent.
  //
  // A row is located as the element it is. The table is laid out as CSS grid,
  // which is why console-interaction.audit.spec.ts addresses rows this way
  // too. What it shows of an id is `shortId` — the first eight characters, an
  // ellipsis, the last four — so the eight-character prefix is what identifies
  // the target on screen.
  const row = page
    .locator('tr[tabindex], [role="row"][tabindex]')
    .filter({ hasText: sandboxId.slice(0, 8) });
  await expect(
    row,
    `exactly one row must be sandbox ${sandboxId}. The page asks for 50 rows, so on ` +
      'any sane host it is on the first one; check GET /api/v1/admin/sandboxes before ' +
      'reading this as a table defect.',
  ).toHaveCount(1);
  await row.click();

  const recordPath = appPath(`/manage/sandboxes/${sandboxId}`);
  await expect(page, 'the row must open the record of the box it names').toHaveURL(
    (url) => url.pathname === recordPath,
  );

  // ── 2. the Session field is a link, and it leads to the right session ─────
  // Attribution comes only from the box's own create metadata — never joined by
  // id shape or create order — so this is the one property the page exists to
  // state, and the only one worth pinning here.
  const sessionLink = card(page, 'Overview').getByRole('link', { name: sessionId, exact: true });
  await expect(
    sessionLink,
    'the box that named a session must link to that session, not to another one',
  ).toHaveAttribute('href', appPath(`/manage/sessions/${sessionId}`));

  await sessionLink.click();
  const sessionPath = appPath(`/manage/sessions/${sessionId}`);
  await expect(page).toHaveURL((url) => url.pathname === sessionPath);
  await expect(
    card(page, 'Overview').getByText(sessionId, { exact: true }),
    'the linked session record must name the same session',
  ).toBeVisible();

  await page.goBack();
  await expect(page).toHaveURL((url) => url.pathname === recordPath);

  // ── 3. the security card states a finding, and Refresh re-asks the box ────
  const security = card(page, 'Network and credentials');
  // The three sentences an operator can be told, from
  // manage:sandboxes.security_default_action / security_none /
  // security_rejected. They are counted rather than matched one at a time
  // because the property under test is that EXACTLY one is on screen: an
  // unverifiable box that renders nothing reads as a contained one, which is
  // the failure this panel's docstring names.
  const contained = security.getByText(/^Default network action: /);
  const unverifiable = security.getByText(
    "AstraBox cannot verify this sandbox's security settings",
  );
  const refused = security.getByText(/^The server refused \(HTTP \d+\)/);
  const securityFindings = async () =>
    (await contained.count()) + (await unverifiable.count()) + (await refused.count());
  await expect
    .poll(securityFindings, {
      message: 'the security card must state exactly one of its three findings, never nothing',
    })
    .toBe(1);

  const securityPath = apiPath(`/admin/sandboxes/${sandboxId}/security`);
  const securityAsked = hits(securityPath);
  expect(securityAsked, 'the card asks the box when it mounts').toBeGreaterThanOrEqual(1);
  await security.getByRole('button', { name: 'Refresh' }).click();
  await expect
    .poll(() => hits(securityPath), {
      message: `Refresh must re-ask ${securityPath}, not redraw the answer it already had`,
    })
    .toBe(securityAsked + 1);

  // ── 4. every diagnostics tab holds a report or a named alert ──────────────
  const diagnostics = card(page, 'Diagnostics');
  for (const { tab } of SCOPES) {
    await diagnostics.getByRole('tab', { name: tab, exact: true }).click();
    // Base UI mounts only the selected panel, so this is the tab just clicked.
    const panel = diagnostics.getByRole('tabpanel');
    const said = async () =>
      (await panel.locator('pre').count())
      + (await panel.getByText(`The sandbox backend does not provide a ${tab} report`).count())
      + (await panel
        .getByText(new RegExp(`^The server rejected the ${tab} request \\(HTTP \\d+\\)$`))
        .count())
      + (await panel.getByText(`Couldn't reach the server for the ${tab} report`).count());
    await expect
      .poll(said, {
        // A report is a live read against the control plane — a log pull, an
        // event query — so this waits longer than the default expect budget,
        // and four scopes at this ceiling still sit inside the spec's.
        timeout: 20_000,
        message:
          `the ${tab} tab must hold a report or exactly one of its three named alerts; ` +
          'an empty panel reads as a sandbox with nothing to say',
      })
      .toBe(1);
  }

  // ── 5. two Refreshes are two requests ─────────────────────────────────────
  // The `requested` ref stops a re-render from asking the control plane the
  // same question twice, and `force: true` is what lets a Refresh through it.
  // One click cannot tell a working Refresh from a report served out of the
  // cache; two can.
  const summaryScope = SCOPES[0];
  await diagnostics.getByRole('tab', { name: summaryScope.tab, exact: true }).click();
  const summaryPath = apiPath(`/admin/sandboxes/${sandboxId}/diagnostics/${summaryScope.wire}`);
  const summaryAsked = hits(summaryPath);
  expect(
    summaryAsked,
    `the panel fetches the ${summaryScope.wire} report on its own`,
  ).toBeGreaterThanOrEqual(1);
  const refreshReport = diagnostics.getByRole('button', { name: 'Refresh' });
  await refreshReport.click();
  await refreshReport.click();
  await expect
    .poll(() => hits(summaryPath), {
      timeout: 20_000,
      message: `two Refreshes must send two more GETs to ${summaryPath}`,
    })
    .toBe(summaryAsked + 2);

  expect(
    uncaught,
    `uncaught exception while reading a sandbox record:\n${uncaught.join('\n')}`,
  ).toEqual([]);
});
