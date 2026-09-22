/**
 * E2E: the agent picker a user left open learns what changed somewhere else.
 *
 * The journey is the ordinary one — leave `/agents` open in one tab while an
 * agent is created, and later deleted, from another seat (a second tab, the
 * CLI, the MCP facade, a colleague). The property is that the picker the user
 * is looking at eventually agrees with the product: a new agent becomes
 * startable, and a deleted one stops offering "Start conversation" on a record
 * that is gone.
 *
 * WHY THIS IS NOT A UNIT TEST'S JOB. `AgentHome` reads the list once per mount
 * — `refresh` is `useCallback(…, [])` and the only effect that calls it depends
 * on `refresh` (AgentHome.tsx:41-53) — and every later read comes from
 * `useKeepCurrent(refresh)` (AgentHome.tsx:54), which re-issues it when the
 * document becomes visible or the window regains focus. A jsdom test can render
 * the component and dispatch an event at it. What it cannot put in one picture
 * is a real browser's visibility and focus behaviour, a second seat writing to
 * the platform, and a picker that must end up agreeing with it — which is the
 * property: the passage of time on a mounted page against a product that
 * changed underneath it.
 *
 * The list is plain `useState`, so the app-wide `revalidateOnFocus: true`
 * (main.tsx:43-45) has no SWR key here to revalidate: that hook is the only
 * producer of a second read, and its 5s throttle is why the returns below are
 * timed rather than dispatched the moment the write has landed.
 *
 * The sibling panel of the same `Tabs` (App.tsx:461-463) carries the same hook
 * with a cadence: AssistantsPage.tsx:80-82 follows every 2s while an assistant
 * is MATERIALIZING. The picker has none, so a return is the whole mechanism.
 *
 * WHY LIST MEMBERSHIP AND NOT THE STATE BADGE. The card also paints a state
 * badge, and "the badge goes stale" looks like the same finding. It is not a
 * transition the backend produces: the only writers of an agent row's `state`
 * are DELETING (agent_service.py:279), ACTIVE (agent_service.py:510) and
 * DELETED (session_read.py:263); `hibernate_agent` (agent_service.py:514-521)
 * writes no state at all, and create fixes it to ACTIVE. A spec waiting for
 * PROVISIONING or HIBERNATING would wait for something the product never emits
 * — two existing specs assert that absence as the invariant
 * (agent-active-no-sandbox-across-hibernate-wake-cycles,
 * agent-stays-active-on-broken-config-no-hibernating-fallback). Membership is
 * the same mechanism, the same complaint, and a change the backend really makes.
 *
 * WHY THE RETURN IS DRIVEN, NOT ONLY CLICKED. A second real tab takes the
 * foreground, which is the journey. But headless Chromium is not guaranteed to
 * flip `visibilityState` for a backgrounded tab, so a hidden phase driven by
 * `bringToFront` alone would be a step that cannot fail. So the tab is really
 * backgrounded AND handed the two APIs a
 * returning user's browser hands it (`visibilitychange`, `focus`) plus a real
 * key press. The assertion is still about the outcome, never the mechanism: an
 * interval while mounted, revalidation on focus, or a push all satisfy it.
 *
 * ENGINE-INDEPENDENT. No conversation, no turn, no sandbox, no pool slot — so
 * it runs under whichever profile `ASTRABOX_E2E_AGENT_NAME` selected, and it is
 * `parallel` because it holds nothing to itself. Every assertion is scoped to
 * this spec's unique `data-agent-name`, so another worker creating or deleting
 * agents cannot flip it; a list-count assertion would be flaky and is avoided.
 *
 * ONE DEPLOYMENT ASSUMPTION WORTH NAMING: `list_all_agents` caps at 200 rows
 * sorted by `updated_at` desc (agent_repository.py:243-251). The created agent
 * sorts newest so it is always in-window; on a deployment carrying more than
 * 200 agents the seeded fence card could fall out of the list and the fence
 * below would fail — as a fence should, loudly, rather than passing blind.
 */
import { expect, test, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';
import { onPassOnly } from '../fixtures/sessionCleanup';

/**
 * How long a returning user may wait for the picker to agree with the product.
 *
 * Generous on purpose. The staleness this spec measures is unbounded — nothing
 * is on a timer, so there is no cadence to out-wait and no reason to make the
 * budget tight. Twenty seconds clears an interval-based fix of any sane period
 * and still leaves both directions, a create and a delete, well inside the
 * lane's 180s wall.
 */
const PICKER_REFRESH_MS = parseTimeoutEnv('ASTRABOX_E2E_AGENT_PICKER_REFRESH_MS', 20_000);

/**
 * `RETURN_THROTTLE_MS` from frontend/src/hooks/useKeepCurrent.ts, plus margin
 * for the skew between the hook stamping its clock and the request it caused
 * reaching this process.
 *
 * The picker's re-read on return is throttled to SWR's `focusThrottleInterval`,
 * so a return delivered inside that window of the picker's last read is dropped
 * and nothing further arrives to re-deliver it. Both returns below are held
 * until the window has passed, which is also the journey: a user who left a tab
 * open while somebody else created an Agent was away for longer than five
 * seconds.
 */
const RETURN_THROTTLE_MS = 5_000;
const THROTTLE_MARGIN_MS = 1_000;

// No session is created here, so there is nothing for `trackSessions()` to
// track; the agent is the only state this spec makes. A failed, timed-out or
// interrupted result keeps it so the scene stays readable — the same rule, in
// its general form (fixtures/sessionCleanup.ts).
let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
  agentId = '';
});

/** Send the picker's tab to the background, observably. */
async function leaveTheTab(page: Page): Promise<void> {
  const state = await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => 'hidden',
    });
    document.dispatchEvent(new Event('visibilitychange'));
    window.dispatchEvent(new Event('blur'));
    return document.visibilityState;
  });
  expect(state, 'the background phase must be observable to the page, or it cannot fail').toBe('hidden');
}

/**
 * Come back to it the way a user does, with every signal a return carries.
 *
 * *readAt* is when the picker last read the Agent list; the return is held
 * until the page's re-read throttle has expired, because a return inside that
 * window is dropped and would be indistinguishable here from a picker that
 * ignores returns altogether. The wait happens while the tab is still hidden,
 * which is where a returning user's absence happens too.
 */
async function returnToTheTab(page: Page, readAt: number): Promise<void> {
  const due = readAt + RETURN_THROTTLE_MS + THROTTLE_MARGIN_MS - Date.now();
  if (due > 0) await page.waitForTimeout(due);
  await page.bringToFront();
  const state = await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => 'visible',
    });
    document.dispatchEvent(new Event('visibilitychange'));
    window.dispatchEvent(new Event('focus'));
    return document.visibilityState;
  });
  expect(state, 'the return must be observable to the page').toBe('visible');
  // One neutral real gesture. A user who clicks into a tab has given the page
  // everything it is going to get; Escape changes nothing on this screen.
  await page.keyboard.press('Escape');
}

test('the open agent picker shows an agent created elsewhere and drops one deleted elsewhere', async ({
  page,
  context,
  request,
}) => {
  // Attached before the first navigation. A crashed React tree also stops
  // rendering new cards, so a red must be able to tell "never learned" from
  // "died", and a listener added later misses the render that mattered.
  const uncaught: string[] = [];
  page.on('pageerror', (error) => uncaught.push(error.message));

  // ── the meter: every agent-list read the PICKER'S TAB issues ─────────────
  // Requests, not responses: the diagnosis this spec exists to print is "the
  // page never asked", which is a statement about what left the tab. Exact
  // pathname equality excludes /agents/{id}/… . `listAgents()` has exactly one
  // caller in the app shell (AgentHome.tsx:42), so every hit here is the
  // picker's own read; the manage console's copies live on other routes, and
  // the second tab below has its own listeners, not this one. Reads the
  // AstraApi fixture makes go through the APIRequestContext, not the page, and
  // are deliberately not counted — which is what lets the control assertions
  // re-ask the platform without disturbing the measurement.
  const listPath = apiPath('/agents');
  const asks: number[] = [];
  page.on('request', (req) => {
    if (req.method() !== 'GET') return;
    if (new URL(req.url()).pathname === listPath) asks.push(Date.now());
  });
  /**
   * When the picker last read the list — the mount read, or the re-read a
   * return caused. The hook throttles the next return against this same read,
   * so the meter doubles as the clock the returns below wait on.
   */
  const lastRead = (): number => asks[asks.length - 1] ?? Date.now();

  // ── setup: a real route for a throwaway agent, no compute ────────────────
  const api = new AstraApi(request);
  const seeded = await api.defaultAgent();
  const seededName = String(seeded.name || '').trim();
  expect(seededName, 'the seeded default Agent must have a name').not.toEqual('');
  const environmentName = String(seeded.environment_name || '').trim();
  expect(environmentName, 'the seeded default Agent must name an Environment').not.toEqual('');
  const models = await api.listEnvironmentModels(environmentName);
  const model = models.find((item) => item && !item.includes('*'));
  expect(model, `${environmentName} must expose a concrete model`).toBeTruthy();
  // Word characters only: the name is read back through an attribute selector.
  const name = `__e2e_picker_freshness_${Date.now()}`;

  const seededCard = page.locator(`[data-testid="agent-option"][data-agent-name="${seededName}"]`);
  const newCard = page.locator(`[data-testid="agent-option"][data-agent-name="${name}"]`);

  // ── the user opens the picker ────────────────────────────────────────────
  await page.goto(appPath('/agents'), { waitUntil: 'domcontentloaded' });
  // The fence. This mount's one read has already ANSWERED before the agent
  // exists, so a later green cannot come from a fetch that raced the create.
  await expect(
    seededCard,
    'the picker must have rendered its list before the Agent exists, or nothing below is a fence',
  ).toBeVisible({ timeout: 30_000 });
  await expect(newCard, 'the picker cannot already know a name nothing has created').toHaveCount(0);

  // The identity of the mount, held three ways, because "it learned" is only
  // interesting if the page the user was looking at is the page that learned.
  const mountedCard = await seededCard.elementHandle();
  expect(mountedCard, 'the picker rendered a card this spec can hold on to').not.toBeNull();
  const mountedAt = await page.evaluate(() => performance.timeOrigin);

  // Prove the meter works before trusting a low reading from it. Mounting the
  // picker always reads the list once, so a zero here means this spec is
  // counting the wrong path — and an unarmed counter reports "never asked"
  // forever. The likely cause is the console's API base having diverged from
  // ASTRABOX_E2E_APP_PREFIX, which no assertion below would notice.
  expect(
    asks.length,
    `no GET ${listPath} was seen while the picker mounted, so this spec is not measuring `
      + 'anything. Check the app prefix before reading a zero as evidence.',
  ).toBeGreaterThanOrEqual(1);

  // ── the other seat ───────────────────────────────────────────────────────
  // A real second tab in the same signed-in context: the picker genuinely goes
  // to the background, which is the journey and the only state in which a
  // focus-driven fix has anything to fire on.
  const other = await context.newPage();
  const otherLoaded = other.waitForResponse((response) => (
    response.request().method() === 'GET'
    && new URL(response.url()).pathname === listPath
  ));
  await other.goto(appPath('/manage/agents'), { waitUntil: 'domcontentloaded' });
  expect((await otherLoaded).ok(), 'the other tab must really be a loaded console page').toBe(true);
  await leaveTheTab(page);

  // ── direction one: something appears ─────────────────────────────────────
  // Creation is setup, not the behaviour under test. `prewarm_enabled: false`
  // is deliberate: create schedules runtime reconciliation
  // (agent_service.py:101-110), and a picker test must never claim a prepared
  // box — the same flag, for the same reason, as
  // agent-config-clears-the-last-list-item.parallel.spec.ts:23.
  const asksBeforeCreate = asks.length;
  const created = await api.createAgent({
    name,
    model,
    environment_name: environmentName,
    prewarm_enabled: false,
  });
  agentId = String(created.agent_id || '');
  expect(agentId, 'the created Agent must have an id').not.toEqual('');

  // The control, taken immediately. Without it a red below could be read as
  // "the record was never created" or "visibility hid it from this user"
  // (private agents are visible to their creator, agent_access.py:110-130)
  // rather than as "the open page never asked again".
  expect(
    (await api.listAgents()).map((agent) => agent.name),
    'the product must publish the new Agent to this same signed-in user',
  ).toContain(name);

  await returnToTheTab(page, lastRead());

  // ── the property ─────────────────────────────────────────────────────────
  // The polled value is the diagnosis: a red prints `{cards: 0, asked: 0}` —
  // the page did not render late, it never asked.
  await expect.poll(async () => ({
    cards: await newCard.count(),
    asked: asks.length - asksBeforeCreate,
  }), {
    timeout: PICKER_REFRESH_MS,
    intervals: [250, 500, 1000],
    message:
      'an agent picker left open must learn about an Agent created in another seat without a '
      + `reload; ${PICKER_REFRESH_MS}ms after the user came back to the tab it still does not `
      + `show ${name}. \`asked\` counts GET ${listPath} issued by this tab since the create. A `
      + '0 means the return produced no read at all: its producer is useKeepCurrent(refresh) '
      + 'at AgentHome.tsx:54, which re-reads on visibilitychange and focus while the document '
      + 'is visible and at most once per 5s — a window the return above waited out. A non-zero '
      + '`asked` with no card means the read happened and what came back did not carry the '
      + 'Agent, which is a platform answer rather than a picker one.',
  }).toMatchObject({ cards: 1 });

  // ── validity guards, before the result is believed ───────────────────────
  expect(
    uncaught,
    `uncaught exception in the picker's tab — a crashed tree renders no cards either:\n${uncaught.join('\n')}`,
  ).toEqual([]);
  await expect(newCard, 'the card must be on screen, not merely in the document').toBeVisible();
  await expect(
    newCard.getByRole('button'),
    'a learned Agent must be startable, not a card without its action',
  ).toBeEnabled();
  expect(
    await page.evaluate(() => performance.timeOrigin),
    'no document reload may stand in for the picker noticing',
  ).toBe(mountedAt);
  expect(
    new URL(page.url()).pathname,
    'the user never navigated — the picker must learn where it already was',
  ).toBe(appPath('/agents'));
  expect(
    await mountedCard!.evaluate((element) => element.isConnected),
    'the card the user was already looking at must be the same DOM node — a picker that '
      + 'remounted (a tab switch, a route change) refetches for free and proves nothing',
  ).toBe(true);

  // ── direction two: something disappears, on the same mount ───────────────
  // A picker that keeps offering "Start conversation" on a deleted Agent hands
  // the user a click that fails. Soft-delete really removes the row from this
  // list's query (`deleted: {$ne: true}`, agent_repository.py:243-251).
  await leaveTheTab(page);
  const asksBeforeDelete = asks.length;
  const deleted = await api.deleteAgent(agentId);
  expect(String(deleted.state || ''), 'delete leaves a historical DELETED Agent').toBe('DELETED');
  agentId = '';
  expect(
    (await api.listAgents()).map((agent) => agent.name),
    'the product must stop publishing the deleted Agent to this same signed-in user',
  ).not.toContain(name);

  await returnToTheTab(page, lastRead());

  await expect.poll(async () => ({
    cards: await newCard.count(),
    asked: asks.length - asksBeforeDelete,
  }), {
    timeout: PICKER_REFRESH_MS,
    intervals: [250, 500, 1000],
    message:
      'an agent picker left open must drop an Agent deleted in another seat without a reload; '
      + `${PICKER_REFRESH_MS}ms after the user came back to the tab it still offers a "Start `
      + `conversation" button for ${name}, whose record is gone. \`asked\` counts GET ${listPath} `
      + 'issued by this tab since the delete, and a 0 has the same single cause as in the '
      + 'create half: the return reached no re-read at AgentHome.tsx:54.',
  }).toMatchObject({ cards: 0 });

  expect(
    uncaught,
    `uncaught exception in the picker's tab while the Agent was deleted:\n${uncaught.join('\n')}`,
  ).toEqual([]);
  await expect(seededCard, 'the picker still holds the agents that did not change').toBeVisible();
  expect(
    await page.evaluate(() => performance.timeOrigin),
    'no document reload may stand in for the picker noticing the delete',
  ).toBe(mountedAt);
  expect(
    new URL(page.url()).pathname,
    'the user never navigated for the delete either',
  ).toBe(appPath('/agents'));
  expect(
    await mountedCard!.evaluate((element) => element.isConnected),
    'the delete must have reached the same mounted picker, not a remounted one',
  ).toBe(true);

  // ── close-out: a fresh mount must agree with the live surface ────────────
  // This is what a repaint-without-refetch "fix" fails.
  await other.close();
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(seededCard).toBeVisible({ timeout: 30_000 });
  await expect(
    newCard,
    'a freshly mounted picker must agree with the live surface about the deleted Agent',
  ).toHaveCount(0);
});
