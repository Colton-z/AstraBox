/**
 * E2E: the operator who pressed "Start workspace" is told when it started.
 *
 * The journey is one click and then nothing. An operator opens a dormant
 * Assistant's record page (`/manage/assistants/<id>`, workspace
 * NOT_MATERIALIZED), presses the one button it offers, and waits — no reload,
 * no navigation, no second click. The record must move on its own: the pill
 * from "Not started" through "Starting" to "Ready", the rail's "Current
 * sandbox" fact from "—" to the box's name, and the lifecycle control back on
 * screen as "Pause workspace". Until it does, the operator is looking at a
 * frozen word with no box named under it and no button but Delete.
 *
 * WHAT MAKES THAT POSSIBLE, AND WHAT THIS SPEC GUARDS. `runAction`
 * (`AssistantDetailPage.tsx:176-187`) still does `await action(); await
 * load();` — exactly one read, taken the instant the wake POST returns. That
 * POST is a request to reach READY and not a wait for it (the contract is
 * stated on `AssistantWorkspace` in `astrabox/api/routes/assistant.py`), so
 * that read can only ever say MATERIALIZING. Everything after it comes from
 * `useKeepCurrent(load, { follow: assistantMaterializing(...) })`
 * (`AssistantDetailPage.tsx:106-108`), which re-reads every
 * `FOLLOW_INTERVAL_MS` while the workspace is in motion and the tab is
 * visible. Remove that one call and this page has no second reader: the pill
 * stays on "Starting" until the operator reloads the tab or walks to a list
 * page that polls.
 *
 * So this is a regression guard, not a reproduction — it is expected GREEN on
 * this tree, and what it guards is one call that reads like a tidy-up. The two
 * sibling surfaces (`AssistantsListPage`, `assistant/AssistantsPage`) carry
 * their own reader and have specs of their own; this is the one surface that
 * carries the Wake/Pause buttons.
 *
 * TWO CAUSES, SEPARATED BY ORDER. A red on "the pill never reached Ready" has
 * two readings — the page never asked again, or the backend never finished
 * materializing — and the long wait cannot tell them apart. So the mechanism
 * is asserted first and cheaply: at least one MORE GET of the record than had
 * been issued when "Starting" appeared, inside `MECHANISM_MS`. That red names
 * the page. The user-seat assertion below it is still the load-bearing one,
 * and it polls the server's own `workspace_state` alongside the pill so its
 * red names the other half (the trail lands in the annotations, which survive
 * a failure).
 *
 * VISIBILITY IS A PRECONDITION, NOT A DETAIL. `useKeepCurrent`'s reader
 * returns without reading when `document.visibilityState` is anything but
 * `visible` (`useKeepCurrent.ts:46`). A hidden page would make every wait
 * below time out while the product was working, so it is asserted before the
 * budget is spent on it.
 *
 * LOCALE IS LOAD-BEARING. `ConsoleRecordPage` renders the pill as
 * `<StatusPill tone={status.tone}>{status.label}</StatusPill>` with no `state`
 * prop (`ConsoleRecordPage.tsx:53-57`), so unlike the list pages' pills this
 * one carries no `data-state` and there is no language-free selector for it.
 * The assertion has to bind to "Not started" / "Starting" / "Ready", and the
 * console detects language as ['localStorage','navigator'] — so both halves
 * are pinned the way the sibling lifecycle specs pin them. The busy label
 * "Starting workspace…" is deliberately NOT asserted on:
 * `manage:assistants.materializing` and `manage:assistants.starting_workspace`
 * are byte-identical, so it cannot distinguish the button mid-POST from the
 * progress note beside a materializing record.
 *
 * NO TIME SIMULATION. The wait here is a real cold provision, not a TTL, so
 * there is nothing to backdate and no worker program to run. The only thing
 * the spec must not do is help: no `page.reload()`, no second `goto`, no
 * second click, and above all no `api.waitForWorkspaceReady()` — that fixture
 * re-POSTs wake in its own loop (`astraApi.ts:549-577`) and would drive the
 * transition this page is supposed to report on its own.
 *
 * WHY EXCLUSIVE. It needs no restart and no shared box — it creates its own
 * Assistant — but it does need one COLD workspace provision to finish inside a
 * fixed 180-second budget, which is capacity. It belongs in the suite
 * contract's one-worker serial group beside the three sibling Assistant
 * workspace specs, so unrelated cold workspaces cannot eat its budget.
 *
 * NOT COVERED, and worth saying out loud: the failure path. A materialization
 * that ends in RECOVERY_REQUIRED is now followed by the same reader, but
 * proving the operator can tell "still starting" from "it failed" needs an
 * induced materialization failure and belongs in its own spec. Nor does this
 * say anything about a workspace changed from outside this browser tab.
 *
 * Deployment fixtures this spec reads and never creates, failing loudly when
 * absent: an enabled Environment running the `assistant` engine
 * (`api.assistantEnvironmentName()`) and `ASTRABOX_E2E_ASSISTANT_MODEL`. The
 * model override is carried only to match the sibling Assistant specs; this
 * journey sends no turn and spends no token, and it reads no engine name, so
 * it is correct under whichever profile the matrix selected.
 */
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';
import { onPassOnly } from '../fixtures/sessionCleanup';

/**
 * How long the page may take to ask a second time.
 *
 * The follower re-reads every 2s (`FOLLOW_INTERVAL_MS`, `useKeepCurrent.ts:15`),
 * so this is seven of its ticks: long enough that a loaded host is not charged
 * for a slow one, short enough that the mechanism's red arrives in seconds
 * rather than after the provisioning wait below.
 */
const MECHANISM_MS = parseTimeoutEnv('ASTRABOX_E2E_ASSISTANT_RECORD_POLL_MS', 15_000);

/**
 * How long a cold Assistant workspace may take to reach Ready on screen.
 *
 * Sized so a clean assertion failure lands before the runner's hard cut rather
 * than as a process-group kill. The waits are sequential and the three ahead of
 * it cap at 50s (15 mount pill + 20 Starting + 15 mechanism), so 110 here sums
 * to 160 and leaves the rest of the lane's 180s wall for the API setup. If a
 * loaded node turns out to provision slower than this, LOWER it and re-scope
 * the spec — raising it converts a readable assertion failure into a run that
 * reads as "the spec hung".
 */
const MATERIALIZE_UI_MS = parseTimeoutEnv('ASTRABOX_E2E_ASSISTANT_MATERIALIZE_UI_MS', 110_000);

/**
 * How long the wake POST and its single read may take to put "Starting" up.
 *
 * Deliberately short. Waking is a request to reach READY, not a wait for it, so
 * a wake that has not answered in twenty seconds is itself the finding — and
 * failing here fast leaves the budget for the assertions that follow.
 */
const STARTING_MS = parseTimeoutEnv('ASTRABOX_E2E_ASSISTANT_STARTING_PILL_MS', 20_000);

// The pill's three readings and the two controls, in the pinned language.
// `manage:assistant_state.*` and `manage:assistants.*` in
// frontend/src/i18n/locales/en/manage.json.
const PILL_NOT_STARTED = 'Not started';
const PILL_STARTING = 'Starting';
const PILL_READY = 'Ready';
const START_WORKSPACE = 'Start workspace';
const PAUSE_WORKSPACE = 'Pause workspace';
const FACT_CURRENT_SANDBOX = 'Current sandbox';
/** What a `ConsoleFact` shows in place of a value it does not have. */
const NO_VALUE = '—';

// The one localized surface this spec reads is the pill, and it carries no
// machine-readable state (see the header). Pin the runner locale and the
// persisted language the app's own switch writes, then assert the label
// exactly — a spec that accepted two spellings would accept a third.
test.use({ locale: 'en-US' });

// Deleting the Assistant destroys its box, so it runs on the passing path only:
// a failed run keeps the workspace, and the annotations below name it.
let assistantId = '';
onPassOnly(async ({ request }) => {
  if (assistantId) await new AstraApi(request).deleteAssistant(assistantId);
});

test('the Assistant record page reaches Ready on its own after Start workspace, with no reload and no second click', async ({
  page,
  request,
}) => {
  // ── observers, attached before the first navigation ───────────────────────
  // A crashed React tree also stops issuing requests and also leaves a stale
  // word on screen, so the counts below need to be able to tell "never asked
  // again" from "died", and a listener added later misses the render that
  // mattered.
  const uncaught: string[] = [];
  page.on('pageerror', (error) => uncaught.push(error.message));

  const gets: string[] = [];
  page.on('request', (req) => {
    if (req.method() === 'GET') gets.push(new URL(req.url()).pathname);
  });
  const hits = (pathname: string) => gets.filter((seen) => seen === pathname).length;

  // The negative control's second half: any navigation of the main frame,
  // same-document ones included. The first half is the epoch stamped on
  // `window` after the goto — it survives a route change and dies with the
  // document, so together they say "this page was never reloaded and never
  // navigated away from".
  let mainFrameNavigations = 0;
  page.on('framenavigated', (frame) => {
    if (frame === page.mainFrame()) mainFrameNavigations += 1;
  });

  // ── setup over the API: arrangement, not the journey ──────────────────────
  const api = new AstraApi(request);
  const runId = new Date().toISOString().replace(/[:.]/g, '-');
  const environmentName = await api.assistantEnvironmentName();
  expect(environmentName, 'an assistant environment must exist').not.toEqual('');
  const assistantModel = await api.assistantModelName(environmentName);

  const assistant = await api.createAssistant({
    display_name: `__e2e_assistant_record_poll_${runId}`,
    environment_name: environmentName,
    model_config_override: { model_name: assistantModel },
  });
  assistantId = String(assistant.assistant_id || '');
  expect(assistantId, 'created assistant must have an id').not.toEqual('');
  test.info().annotations.push({ type: 'e2e_assistant_id', description: assistantId });

  // The journey's premise, asserted rather than assumed — and read through GET
  // rather than taken from the create response, which carries no workspace
  // projection at all (`create_assistant` returns `_sanitize_assistant(row)`).
  // Asserting the absent key with a `|| 'NOT_MATERIALIZED'` fallback would be a
  // check that cannot fail.
  const dormant = await api.getAssistant(assistantId);
  expect(
    String(dormant.workspace_state || ''),
    'this journey starts at a workspace nobody has built yet',
  ).toEqual('NOT_MATERIALIZED');
  expect(
    String(dormant.current_sandbox_id || '').trim(),
    'a workspace that was never built holds no box',
  ).toEqual('');

  // Pin the console language before the FIRST navigation, or the pill below
  // reads whatever the runner's locale happens to be (see test.use above).
  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* localStorage unavailable — the en-US navigator locale still applies */
    }
  });

  // ── the record, opened directly ───────────────────────────────────────────
  // A deep link, deliberately not a click through /manage/assistants: that list
  // has a reader of its own, and arriving from it would leave a reader of this
  // spec unable to say which surface refreshed what. This is the last
  // navigation performed here.
  const detailPath = apiPath(`/assistants/${assistantId}`);
  await page.goto(appPath(`/manage/assistants/${assistantId}`));

  const epoch = `record-page-${runId}`;
  await page.evaluate((value: string) => {
    (window as unknown as { __e2eRecordPageEpoch?: string }).__e2eRecordPageEpoch = value;
  }, epoch);
  const navigationsAtArrival = mainFrameNavigations;

  // One pill on this route: `AssistantDetailPage` renders exactly one through
  // ConsoleRecordPage, and the manage shell renders none. If a future shell
  // adds one, scope this to the heading row rather than loosening the match.
  const pill = page.getByTestId('status-pill');
  /** A rail fact's value — the quiet label's own next sibling (`ConsoleFact`). */
  const railFact = (label: string) =>
    page.getByText(label, { exact: true }).locator('xpath=following-sibling::div[1]');
  const currentSandbox = railFact(FACT_CURRENT_SANDBOX);

  // A record that has not rendered its pill in fifteen seconds is not slow, it
  // is broken — and every budget below is spent after this one.
  await expect(pill, 'the dormant record reads Not started').toHaveText(PILL_NOT_STARTED, {
    timeout: 15_000,
  });
  await expect(
    currentSandbox,
    'a workspace nobody has started names no box',
  ).toHaveText(NO_VALUE);

  // Prove the meter is armed before any low reading from it is believed. The
  // page must have read the record to render it at all, so a zero here means
  // this spec is counting the wrong path — most likely the console's API base
  // having diverged from ASTRABOX_E2E_APP_PREFIX — and an unarmed counter
  // reports perfect silence forever.
  expect(
    hits(detailPath),
    `no GET ${detailPath} was seen while the record rendered, so this spec is not `
      + 'measuring anything. Check the app prefix before reading any count below as news.',
  ).toBeGreaterThanOrEqual(1);

  // The follower reads only while the document is visible
  // (`useKeepCurrent.ts:46`). Asserted here so a hidden page fails as a hidden
  // page, instead of timing out below and reading as a product defect.
  expect(
    await page.evaluate(() => document.visibilityState),
    'the record page must be visible: the reader that keeps it current returns '
      + 'without reading on a hidden document, and every wait below would then be '
      + 'measuring the harness',
  ).toEqual('visible');

  // ── the only input in the whole journey ───────────────────────────────────
  const startButton = page.getByRole('button', { name: START_WORKSPACE });
  await expect(startButton, 'a dormant workspace offers its one action').toBeEnabled();
  await startButton.click();

  // The state the operator is left sitting on, and the premise of everything
  // below: if wake ever returned only after materialization had finished, the
  // pill would go straight to Ready and none of this would be evidence. The
  // API says it cannot — waking is a request to reach READY, not a wait for
  // it — so a red here means that contract changed.
  await expect(
    pill,
    `the pill never reached "${PILL_STARTING}" after Start workspace. Either the wake POST `
      + 'did not answer inside this window — it is contracted to return as soon as it has '
      + 'a provisioning session, not when the box is up — or the workspace was already '
      + 'materialized, in which case this run proves nothing about a page waiting on one.',
  ).toHaveText(PILL_STARTING, { timeout: STARTING_MS });

  // ── mechanism: the page asks again, on its own ────────────────────────────
  // A delta, never an exact count: the page may read more than once on the way
  // here, and `runAction`'s own read has already landed by the time the pill
  // says Starting. What must be true is that a read arrives AFTER this line.
  const readsAtStarting = hits(detailPath);
  await expect
    .poll(() => hits(detailPath), {
      timeout: MECHANISM_MS,
      intervals: [500, 500, 1_000],
      message:
        `the record page issued no GET ${detailPath} after the wake POST's own read. `
        + 'That single read (AssistantDetailPage.tsx:176-187, `await action(); await load();`) '
        + 'can only ever say MATERIALIZING, so a page with no second reader freezes on '
        + '"Starting" until someone reloads it. The second reader is '
        + '`useKeepCurrent(load, { follow: assistantMaterializing(...) })` at '
        + 'AssistantDetailPage.tsx:106-108 — check it still exists and that `follow` is '
        + 'true for MATERIALIZING.',
    })
    .toBeGreaterThan(readsAtStarting);

  // ── the user's seat: with no further input of any kind ────────────────────
  // The server's own view is sampled alongside the pill and recorded as it
  // changes, so a red here names which half failed: a server that reached
  // READY while the pill stayed on Starting is the page; a server that never
  // left MATERIALIZING is the substrate.
  const serverTrail: string[] = [];
  const noteServerState = async () => {
    let state: string;
    try {
      state = String((await api.getAssistant(assistantId)).workspace_state || '<none>');
    } catch (error) {
      // "We could not ask" and "it did not move" send a reader to different
      // places, so a failed read is recorded as itself rather than swallowed.
      state = `<read failed: ${String(error).slice(0, 120)}>`;
    }
    if (serverTrail[serverTrail.length - 1] !== state) {
      serverTrail.push(state);
      test.info().annotations.push({ type: 'e2e_workspace_state', description: state });
    }
    return state;
  };

  await expect
    .poll(
      async () => {
        await noteServerState();
        return ((await pill.textContent()) || '').trim();
      },
      {
        timeout: MATERIALIZE_UI_MS,
        intervals: [1_000, 2_000, 2_000],
        message:
          'an operator who pressed Start workspace and then did nothing must be told when '
          + 'the workspace came up. The pill never reached "Ready" with no reload and no '
          + 'second click. The `e2e_workspace_state` annotations carry what the server said '
          + 'while this waited: if it reached READY, the page stopped reading '
          + '(AssistantDetailPage.tsx:106-108); if it did not, the workspace never '
          + 'materialized and this is a provisioning failure, not a console one.',
      },
    )
    .toBe(PILL_READY);

  // ── the rest of the record caught up, not just the word ───────────────────
  const settled = await api.getAssistant(assistantId);
  const sandboxId = String(settled.current_sandbox_id || '').trim();
  expect(
    sandboxId,
    'a READY workspace must name the box it holds, or the rail has nothing true to show',
  ).not.toEqual('');
  test.info().annotations.push({ type: 'e2e_assistant_sandbox_id', description: sandboxId });
  await expect(
    currentSandbox,
    'the rail must name the box the platform says this workspace holds — equality, because '
      + '"no longer —" would also be satisfied by the page printing something else',
  ).toHaveText(sandboxId);

  // The operator got their controls back, not just a word: both lifecycle
  // buttons are absent while MATERIALIZING, and Pause is rendered only for
  // READY (`assistantHibernatable`).
  const pauseButton = page.getByRole('button', { name: PAUSE_WORKSPACE });
  await expect(
    pauseButton,
    'a Ready workspace offers Pause again — while it was starting there was no lifecycle '
      + 'control on the page at all',
  ).toBeVisible();
  await expect(pauseButton).toBeEnabled();

  // ── negative control: none of this was bought with a refresh ──────────────
  expect(
    await page.evaluate(
      () => (window as unknown as { __e2eRecordPageEpoch?: string }).__e2eRecordPageEpoch ?? null,
    ),
    'the document this journey started in must be the one that finished it. A lost epoch '
      + 'means the tab was reloaded, which is exactly the help this spec must not have had. '
      + 'The one thing in the app that can reload a tab by itself is the frontend release '
      + 'guard (frontendRelease.ts:64) — a deploy landing mid-run would show up here.',
  ).toEqual(epoch);
  expect(
    mainFrameNavigations - navigationsAtArrival,
    'no navigation of any kind may follow the click: the spec calls no page.reload() and no '
      + 'second goto, so a green result cannot have come from a refresh',
  ).toEqual(0);

  // A record that reached Ready inside a crashed tree would still read Ready.
  expect(
    uncaught,
    `uncaught exception while the record page waited on its workspace:\n${uncaught.join('\n')}`,
  ).toEqual([]);

  await test.info().attach('assistant-record-page-freshness', {
    body: JSON.stringify(
      {
        assistantId,
        sandboxId,
        environmentName,
        detailPath,
        recordReads: {
          atStarting: readsAtStarting,
          atReady: hits(detailPath),
        },
        serverStateTrail: serverTrail,
        mainFrameNavigationsAfterArrival: mainFrameNavigations - navigationsAtArrival,
        budgetsMs: {
          starting: STARTING_MS,
          mechanism: MECHANISM_MS,
          materializeUi: MATERIALIZE_UI_MS,
        },
      },
      null,
      2,
    ),
    contentType: 'application/json',
  });
});
