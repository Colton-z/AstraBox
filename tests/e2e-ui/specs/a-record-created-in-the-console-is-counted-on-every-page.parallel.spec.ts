/**
 * E2E: the console rail counts the record the operator just made — on every
 * page, not only on the one list that happens to be open.
 *
 * The journey is the first thing anybody does in this console: open Agents,
 * press New agent, fill the form, press Create. The product answers by landing
 * the reader on the record it just made, and the quiet number beside "Agents"
 * in the rail has to include it there, on that record page, and on every other
 * section the reader walks to afterwards — in a tab that was never reloaded.
 *
 * WHY THIS IS NOT A UNIT TEST'S JOB. The rail reads two different sources and
 * switches between them on the URL (ManageApp.tsx:166-176): while `pathname ===
 * '/manage/agents'` the badge shows what the open list published into
 * `MANAGE_NAV_COUNT_KEYS.agents` (AgentsListPage.tsx:53), and everywhere else
 * it shows `inactive?.agents` from the `adminNavigationSummary` read
 * (ManageApp.tsx:145-148), which `ManageSidebar` revalidates whenever the
 * pathname changes (ManageApp.tsx:152-158). `ManageSidebar` is `AppShell`'s
 * `sidebarContent` — outside the inner `<Routes>` — so client-side navigation
 * re-renders it and never remounts it, which makes that revalidation the only
 * thing that can move the number: the `mutate` the create page fires
 * (AgentCreatePage.tsx:169) names `MANAGE_NAV_COUNT_KEYS.agents`, whose hook is
 * declared with a `null` fetcher (ManageApp.tsx:162), so it has nothing to call
 * and does not touch the summary key. A jsdom test renders either branch and
 * sees it render the number it was handed; the property here is a write, a
 * navigation and two cache keys, across a shell that stays mounted.
 *
 * WHY THE TAB MUST NOT BE REFRESHED — READ BEFORE EDITING. A reload or a full
 * navigation remounts the shell and re-reads the summary outright, and
 * `revalidateOnFocus: true` (main.tsx:45) re-reads it on a window focus or a
 * visibilitychange. Either makes the badge current for a reason that has
 * nothing to do with the property under test. So this spec issues exactly ONE
 * `page.goto`, never reloads, never opens a second tab and never calls
 * `bringToFront`: a `click` on a rail link is a client-side route change and
 * dispatches no window focus. Both cures are watched rather than assumed — the
 * tab carries a marker a document replacement would take with it, and a
 * listener installed at document start records the two signals SWR acts on
 * (`initFocus` in the swr package: a non-capturing `window` 'focus' and a
 * `document` 'visibilitychange'). A measurement taken after either arrives is
 * thrown out BY NAME. Element focus is recorded and does NOT void the run:
 * `focus` does not bubble, so SWR's window listener cannot see the anchor a
 * rail click focuses, and voiding on it would make every reading after this
 * spec's own navigation unreadable. console-sidebar-counts.audit.spec.ts:51 is
 * blind to this property for the opposite reason — it re-`goto`s before reading
 * each badge, so it only ever compares a freshly mounted shell with a freshly
 * loaded list. (It is also an audit spec, so it is not in the release lane.)
 *
 * WHAT AN EXACT GLOBAL COUNT COSTS IN A PARALLEL LANE. This spec never names a
 * number: every read polls the badge against the deployment's own live count in
 * the same breath. The two counts are the same population by construction — the
 * badge sums `can_view_agent` over `list_agent_access_docs()`
 * (admin_service.py:588) and `GET /api/v1/agents` filters `list_all_agents()`
 * by `can_view_agent` (agent_service.py:162-165), and both repository reads are
 * the same query with the same `limit=200` and the same `{"deleted": {"$ne":
 * True}}` filter (agent_repository.py:274-296) — so they cannot diverge on a
 * large deployment either. What they can diverge in is TIME: the badge holds
 * the count as of the navigation that revalidated it, so an Agent another
 * worker creates after that read is in the live count and not in the badge, and
 * the poll then runs out against a correct product. The exposure is the second
 * or so between each navigation's summary read and the poll's first agreement,
 * and a console Agent write by a sibling spec inside it is the first thing a
 * red here should be checked against. The mirror case is the false green:
 * another worker deleting exactly one Agent inside a poll window restores the
 * frozen number by coincidence.
 *
 * WHAT THIS SPEC DOES NOT COVER, named rather than implied. The Sessions badge
 * is the harder instance of the same mechanism — conversations are created and
 * ended by product traffic with no console action at all, so away from
 * `/manage/sessions` that number moves only when the reader navigates. Proving
 * it needs a conversation and a sandbox, which is not this lane; it belongs in
 * its own spec. The delete direction is not covered either, and deliberately:
 * `deleteAgent` (frontend/src/api.ts:999) has no call site in `frontend/src`,
 * so there is no console delete to drive, and staging one through the API would
 * assert a different property — that the rail follows writes made ELSEWHERE —
 * which a fix for this one need not deliver.
 *
 * ENGINE-INDEPENDENT. No conversation, no turn, no sandbox. A page-created
 * Agent leaves `prewarm_enabled` false (astrabox_models.py:127; the form folds
 * it away as advanced and this spec never opens that disclosure), so it
 * reserves no prepared slot and holds nothing another worker needs. It runs
 * under whichever profile `ASTRABOX_E2E_AGENT_NAME` selected; all it asks of
 * that profile is that its Environment be one the create form offers, which it
 * asserts by name before selecting.
 */
import { test, expect } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { onPassOnly } from '../fixtures/sessionCleanup';

declare global {
  interface Window {
    /**
     * `'alive'` from just after this spec's only navigation. Set with
     * `page.evaluate`, never from an init script, so a reload or a full
     * navigation loses it — which is the whole point: those are the two things
     * that would heal the rail and hide the defect.
     */
    __consoleRailTab?: string;
    /**
     * Focus-family signals seen by this tab, appended by the init script.
     * `revalidates` is true for the two SWR itself listens to.
     */
    __consoleRailSignals?: { type: string; at: number; revalidates: boolean }[];
    /** When the write landed. Signals before it are the page opening. */
    __consoleRailWatchFrom?: number | null;
  }
}

const runId = Date.now();
const AGENT_NAME = `__e2e_rail_count_${runId}`;

/**
 * How long the rail may take to agree with the deployment after a write.
 *
 * A product that invalidates the summary on a console write converges inside
 * one request; a frozen one never does, so this only decides how long a red
 * takes to arrive.
 */
const RAIL_AGREEMENT_MS = parseTimeoutEnv('ASTRABOX_E2E_RAIL_COUNT_MS', 10_000);

/**
 * How long Create may take to land on its record.
 *
 * The same name and the same fallback the API fixture uses for the same
 * operation (astraApi.createAgent), so the lane tunes one value rather than
 * two that can disagree.
 */
const CREATE_LANDS_MS = parseTimeoutEnv('ASTRABOX_E2E_AGENT_CREATE_API_TIMEOUT_MS', 120_000);

// The one localized surface here is the console's own chrome — the rail links,
// the form's field labels and the two buttons. The console detects language as
// ['localStorage','navigator'], so an unpinned runner locale is what decides
// which language they are in, and a spec accepting two spellings would accept a
// third. Pinned the way the lifecycle specs pin it: a fixed navigator locale
// plus the persisted 'astrabox-lang' the app's own switch writes.
test.use({ locale: 'en-US' });

let createdAgentId = '';

// The Agent outlives a failure on purpose (see fixtures/sessionCleanup): a
// record deleted in teardown is a record nobody can look at afterwards — and a
// red here is precisely a claim about how many of them the deployment holds.
onPassOnly(async ({ request }) => {
  if (createdAgentId) await new AstraApi(request).deleteAgent(createdAgentId);
});

test('a record created in the console is counted by the rail on every page, without a reload', async ({
  page,
  request,
}) => {
  const uncaught: string[] = [];
  // Before the first navigation: a tree that crashed during a render shows no
  // badge either, and "no badge" must not be reported as a stale count.
  page.on('pageerror', (error) => uncaught.push(error.message));

  /** Every navigation-summary read this tab issues, for the failure text only. */
  const summaryFetches: number[] = [];
  page.on('request', (r) => {
    if (r.url().includes('/admin/navigation-summary')) summaryFetches.push(Date.now());
  });

  const api = new AstraApi(request);

  // ── Setup. The API is allowed here and, below, only as the oracle ────────
  const seeded = await api.defaultAgent();
  const environmentName = String(seeded.environment_name || '').trim();
  expect(
    environmentName,
    'the matrix-selected Agent must name the Environment this spec picks in the form',
  ).not.toEqual('');

  // The model is taken from the catalogue the FORM reads
  // (GET /admin/environments/{name}/models), not from the seeded Agent's pin: a
  // pin the gateway does not enumerate lands in the combobox as a custom value,
  // and the option asserted below would never appear.
  const offered = await api.listEnvironmentModels(environmentName);
  const seededModel = String(seeded.model || '').trim();
  const model = offered.includes(seededModel)
    ? seededModel
    : (offered.find((candidate) => candidate && !candidate.includes('*')) ?? '');
  expect(
    model,
    `Environment ${JSON.stringify(environmentName)} offers the create form no concrete `
      + `model to pick; it returned: ${offered.join(', ') || '(nothing)'}`,
  ).not.toEqual('');

  // Pin the console language before the FIRST navigation (see test.use above).
  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* localStorage unavailable — the en-US navigator locale still applies */
    }
  });
  // The two listeners SWR registers for `revalidateOnFocus`, mirrored exactly
  // (`initFocus` in the swr package: a non-capturing `window` 'focus' and a
  // `document` 'visibilitychange'), plus a capture-phase reader for the element
  // focus a rail click leaves on its anchor. `focus` does not bubble, so a
  // non-capturing window listener — SWR's included — only ever fires for the
  // window itself; the element half is evidence and never voids a reading.
  // Installed at document start, so nothing that reaches SWR reaches it unseen.
  await page.addInitScript(() => {
    window.__consoleRailSignals = [];
    window.__consoleRailWatchFrom = null;
    const record = (type: string, revalidates: boolean) => {
      window.__consoleRailSignals?.push({ type, at: Date.now(), revalidates });
    };
    window.addEventListener('focus', () => record('window-focus', true));
    document.addEventListener('visibilitychange', () => record('visibilitychange', true));
    window.addEventListener(
      'focus',
      (event) => {
        if (event.target === window) return;
        record('element-focus', false);
      },
      true,
    );
  });

  // ── THE ONLY page.goto IN THIS SPEC ─────────────────────────────────────
  await page.goto(appPath('/manage/agents'));
  await page.evaluate(() => {
    window.__consoleRailTab = 'alive';
  });

  const rail = page.locator('[data-slot="sidebar"]');
  // A collapsed rail has no badge to read at all: SidebarMenuBadge carries
  // `group-data-[collapsible=icon]:hidden` (ui/sidebar.tsx:592). The provider
  // opens expanded and nothing reads the stored cookie back, so this is a
  // guard rather than a step — but a guard that says which of the two it was.
  if ((await rail.getAttribute('data-state')) === 'collapsed') {
    await page.locator('[data-slot="sidebar-trigger"]').first().click();
  }
  await expect(
    rail,
    'the rail must be expanded for any of its counts to be on screen',
  ).toHaveAttribute('data-state', 'expanded');

  // The SurfaceNav "Console" row points at the same route, so the menu item is
  // identified by the badge it owns rather than by the href alone.
  const railAgentsBadge = rail.locator(
    '[data-slot="sidebar-menu-item"]:has(a[href$="/manage/agents"]) [data-slot="sidebar-menu-badge"]',
  );
  const railLink = (name: string) => rail.getByRole('link', { name, exact: true });

  /** The collection total the open list page states beside its own title. */
  const statedTotal = async (where: string): Promise<number> => {
    const meta = page.getByRole('heading', { level: 1 }).locator('xpath=following-sibling::span[1]');
    const text = (await meta.innerText()).trim();
    const digits = text.match(/\d+/);
    expect(digits, `${where}: the page must state a collection total beside its title: ${text}`)
      .not.toBeNull();
    return Number(digits![0]);
  };

  /**
   * The tab that made the write is still the tab doing the reading.
   *
   * Both halves matter and neither is redundant. A lost marker means the
   * document was replaced — the summary was re-read for a reason that has
   * nothing to do with this property, and the measurement is void rather than
   * green. A window focus or a visibilitychange after the write means
   * `revalidateOnFocus` re-read the summary for the same reason. Signals
   * recorded before the write are the page opening and are ignored, and the
   * element focus a rail click leaves behind is recorded without voiding
   * anything: SWR's non-capturing window listener cannot see it.
   */
  const tabMustStillBeTheOneThatWrote = async (where: string) => {
    const seen = await page.evaluate(() => {
      const from = window.__consoleRailWatchFrom;
      return {
        marker: window.__consoleRailTab ?? null,
        armed: typeof from === 'number',
        since: (window.__consoleRailSignals ?? [])
          .filter((s) => s.at >= (from ?? Infinity))
          .filter((s) => s.revalidates),
      };
    });
    expect(
      seen.marker,
      `${where}: this tab's marker is gone, so the document was replaced — a reload or a full `
        + 'navigation re-mounts the shell and re-reads the summary. This measurement is void, '
        + 'not green.',
    ).toEqual('alive');
    expect(seen.armed, `${where}: the write marker was never armed, so nothing below is fenced`)
      .toBe(true);
    expect(
      seen.since.map((s) => s.type),
      `${where}: a window focus or visibilitychange reached the tab after the write, and `
        + 'revalidateOnFocus (main.tsx:45) re-reads the navigation summary on either. Whatever '
        + 'the badge says now, it is not evidence about a tab nobody touched.',
    ).toEqual([]);
  };

  /**
   * The rail's Agents count is the deployment's, read where the operator is.
   *
   * Polled against the live count rather than against a number this spec
   * predicted: a rail that re-reads the summary on the reader's own navigation
   * agrees whatever the deployment held when this spec started, and a frozen
   * one cannot be rescued by another worker's write. What it cannot absorb is a
   * sibling Agent write landing after the navigation that fed the badge — see
   * the header's note on time skew.
   */
  const railMustAgreeWithTheDeployment = async (where: string) => {
    const asked = summaryFetches.length;
    await expect
      .poll(
        async () => {
          // `allTextContents` rather than `textContent`: a missing badge is a
          // real outcome here (the summary read failing renders none), and it
          // must report itself in a poll interval instead of waiting out the
          // action timeout on every attempt.
          const shown = await railAgentsBadge.allTextContents();
          const badge = shown.length === 1 ? shown[0].trim() : `(${shown.length} badges)`;
          const truth = String((await api.listAgents()).length);
          return badge === truth ? 'agrees' : `rail says ${badge}, the deployment holds ${truth}`;
        },
        {
          timeout: RAIL_AGREEMENT_MS,
          intervals: [500, 1000, 2000],
          message:
            `${where}: the rail's Agents count must be the deployment's, in a tab that was `
            + 'never reloaded. This tab has asked for /admin/navigation-summary '
            + `${asked} time(s) so far; if that number never moves the shell simply kept the `
            + 'count it last read — ManageApp.tsx:169 reads `inactive?.agents` on every path '
            + 'but /manage/agents, and the pathname effect at ManageApp.tsx:152-158 is what '
            + 'revalidates MANAGE_NAV_SUMMARY_KEY behind it.',
        },
      )
      .toBe('agrees');
  };

  // ── Precondition, on the page that owns the number ───────────────────────
  // AgentsListPage disables Refresh while its request is in flight
  // (AgentsListPage.tsx:164) and publishes its total on the way out, so this is
  // the settle signal the list itself offers. No networkidle: this is a lane spec.
  await expect(
    page.getByRole('button', { name: 'Refresh' }),
    'the Agents list must finish loading before it can publish a total',
  ).toBeEnabled();
  await expect(
    railAgentsBadge,
    'the rail must be showing an Agents count for any of this to check anything',
  ).toHaveCount(1);
  const openTotal = await statedTotal('the Agents list');
  await expect(
    railAgentsBadge,
    'before anything is written the badge must be the number this page itself states — '
      + 'otherwise the value polled below is not the value the product shows',
  ).toHaveText(String(openTotal));

  // ── The write, made the way an operator makes it ─────────────────────────
  // The header and the empty state offer the same action under the same words.
  await page.getByRole('button', { name: 'New agent' }).first().click();
  await expect(page).toHaveURL((url) => url.pathname === appPath('/manage/agents/new'));

  // By accessible name, not `getByLabel`: a required console field puts an
  // `aria-hidden` asterisk inside its label, so an exact "Name" there matches
  // nothing (ConsoleForm.tsx:149-153).
  const nameField = page.getByRole('textbox', { name: 'Name', exact: true });
  await expect(nameField, 'the create form must render before it can be filled').toBeVisible();
  await nameField.fill(AGENT_NAME);

  // `environment_name` is required and not advanced (agent_schema.py:175), so
  // it is on screen. The picker is filtered to enabled + engine_available +
  // agent_chat environments (agentEditConfig.ts:147-160): if the matrix's own
  // Environment is not among them, fail by name rather than select nothing.
  const environmentField = page.getByRole('combobox', { name: 'Runtime environment', exact: true });
  await expect(
    environmentField.locator(`option[value="${environmentName}"]`),
    `the create form must offer the Environment ${JSON.stringify(environmentName)} that the `
      + 'matrix-selected Agent runs on',
  ).toHaveCount(1);
  await environmentField.selectOption(environmentName);

  // `model` is required too (agent_schema.py:135, enforced by
  // schema_validation.validate_payload:111-114) while the page blocks only on a
  // missing name (AgentCreatePage.tsx:159), so an unfilled model would reach the
  // server and come back as an error card instead of a record. The field is the
  // searchable picker: clicking it opens the list and typing filters it, and
  // its options only exist once the environment above settled the catalogue.
  const modelField = page.getByRole('combobox', { name: 'Model', exact: true });
  await modelField.click();
  await modelField.fill(model);
  const modelOption = page.getByRole('option', { name: model, exact: true });
  await expect(
    modelOption,
    `the model catalogue for ${JSON.stringify(environmentName)} must offer ${model} in the form, `
      + 'as the admin listing said it does',
  ).toBeVisible();
  await modelOption.click();
  await expect(modelField).toHaveValue(model);

  const create = page.getByRole('button', { name: 'Create', exact: true });
  await expect(create, 'a filled create form must offer its Create').toBeEnabled();
  await create.click();

  // Create lands on the record, not back on the list (AgentCreatePage.tsx:161-173),
  // which is what makes the very next screen a page the rail gets wrong.
  await expect(
    page,
    'Create must land on the Agent it made, keyed by the id the server minted',
  ).toHaveURL(/\/manage\/agents\/[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/, {
    timeout: CREATE_LANDS_MS,
  });
  createdAgentId = new URL(page.url()).pathname.split('/').pop() ?? '';
  expect(createdAgentId, 'the created Agent must have an id').not.toEqual('');

  // Arm the fence: from here on, a focus signal or a replaced document voids
  // every reading rather than passing it.
  await page.evaluate(() => {
    window.__consoleRailWatchFrom = Date.now();
  });

  // ── READ 1: the record's own page, the screen Create lands on ────────────
  await tabMustStillBeTheOneThatWrote('READ 1');
  await railMustAgreeWithTheDeployment('the Agent record page reached straight from Create');

  // ── READ 2: the stated journey — walk the rail to another section ────────
  await railLink('Environments').click();
  await expect(page).toHaveURL((url) => url.pathname === appPath('/manage/environments'));
  await tabMustStillBeTheOneThatWrote('READ 2');
  await railMustAgreeWithTheDeployment(
    'the Environments page, reached by clicking the rail after creating an Agent',
  );

  // ── Control: the page that owns the number states it from its own read ──
  // What gives the reads around it their authority. On this one route the badge
  // comes from the list's own response, so a green here says the count is
  // reachable and this tab can see the record it just wrote — which is what
  // makes a red on any other route a statement about the rail's summary rather
  // than about the write.
  await railLink('Agents').click();
  await expect(page).toHaveURL((url) => url.pathname === appPath('/manage/agents'));
  await expect(page.getByRole('button', { name: 'Refresh' })).toBeEnabled();
  await expect(
    page.getByRole('row').filter({ hasText: AGENT_NAME }),
    'the Agent created through the form must be listed for the user who created it — the truth '
      + 'here is an observed record, not arithmetic',
  ).toHaveCount(1);
  const listTotal = await statedTotal('the Agents list after the create');
  await expect(
    railAgentsBadge,
    'on its own list page the badge must be that page\'s stated total',
  ).toHaveText(String(listTotal));
  await tabMustStillBeTheOneThatWrote('control');

  // ── READ 3: walking away must not throw the truth away ───────────────────
  // The branch where the number visibly counts DOWN while the operator
  // navigates and nothing was deleted: the list published the right total into
  // one cache key, and leaving the route goes back to the other one.
  await railLink('Environments').click();
  await expect(page).toHaveURL((url) => url.pathname === appPath('/manage/environments'));
  await tabMustStillBeTheOneThatWrote('READ 3');
  await railMustAgreeWithTheDeployment(
    'Environments, after the Agents list had already published the correct total',
  );

  expect(
    uncaught,
    `uncaught exception while walking the console rail:\n${uncaught.join('\n')}`,
  ).toEqual([]);
});
