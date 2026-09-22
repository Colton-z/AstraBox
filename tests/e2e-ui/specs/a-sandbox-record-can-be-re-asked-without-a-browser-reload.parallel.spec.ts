/**
 * E2E: the record of one sandbox must stop claiming the box is alive.
 *
 * THE JOURNEY. An operator opens the record of the box a conversation is using
 * — `/manage/sandboxes/<id>` — and leaves it on screen. The compute is
 * reclaimed out of band. The record is a one-shot read, so on its own it goes
 * on saying RUNNING with a breathing dot, and every judgement the operator
 * makes from that page is made about a box the control plane does not run.
 * The product answer under test is that coming back to the tab makes the record
 * re-ask and tell the truth — no browser reload, no second navigation, no
 * click.
 *
 * WHY THE RECORD AND NOT THE LIST. The list has its own spec
 * (`a-returning-operator-never-acts-on-a-sandbox-that-is-gone.parallel`), and
 * that spec deliberately stops at the row: clicking through to the record
 * "passes whether or not the list ever re-read". This is the other half, from
 * the other direction — the record page already open, never navigated to again.
 * It is also the only console surface that makes a per-box LIVENESS claim: the
 * title pill is `data-pulse="true"` whenever the box's state is RUNNING
 * (`ConsoleRecordPage.tsx:53-57` renders `StatusPill` with no `live` prop, and
 * `AstraConsole.tsx:118` defaults `isLive` to `tone === 'running'`, which
 * `sandboxConfig.ts:24-30` gives RUNNING). A pulse is a statement about now.
 *
 * WHAT THE PRODUCT DOES. `SandboxDetailPage` holds its read in `useState` and
 * re-issues it from `useKeepCurrent(load)` (`SandboxDetailPage.tsx:73`), which
 * re-reads on `visibilitychange` and `focus`, throttled to `RETURN_THROTTLE_MS`
 * (5s, SWR's `focusThrottleInterval`). That call is the page's only second
 * reader: without it the record answers once per mount, and the browser's own
 * reload is the whole remedy. So this is a REGRESSION GUARD, expected green on
 * this tree, over one line that reads like a tidy-up.
 * Two of its properties shape every step: the return is dropped if it arrives
 * inside the throttle (so the return here is timed, not sent the instant the
 * page settles), and the page has no `follow` cadence — an operator who never
 * leaves the tab is given no return to re-read on, exactly as on the lists.
 *
 * NO TIME HOOK IS NEEDED. Nothing here has a TTL to backdate and no sweep to
 * drive: the staleness starts the moment the response lands and lasts as long
 * as the tab is open, so the operator's forty-minute absence contributes
 * nothing the reclaim below does not supply directly.
 *
 * THE ORACLE IS THE DEPLOYMENT'S OWN ANSWER, asked through the same listing the
 * page reads (`adminListSandboxes`, `SandboxDetailPage.tsx:59`). Until the
 * backend itself stops reporting the box as RUNNING, the console cannot be
 * called wrong — a red before that point is a reclaim question, not a console
 * one. The verdict then branches on what the deployment says at that moment
 * rather than accepting two spellings for one state: a box the listing has
 * dropped must render the page's not-found card, and a box it still carries
 * must be labelled with the backend's own state string.
 *
 * FALSE-GREEN GUARDS, each for a way this could pass while proving nothing:
 *  1. the armed counter. A zero GET count for the record's own read while the
 *     record is on screen means this spec is measuring the wrong path (an app
 *     prefix mismatch), and an unarmed counter reports perfect silence forever;
 *  2. the BEFORE pulse. "No pulsing pill" is also what a page that never
 *     loaded says, so the live box must be caught pulsing first;
 *  3. the epoch and the main-frame navigation counter. The release guard
 *     listens on the same two events (`frontendRelease.ts:117-118`) and
 *     replaces the document when the served entry hash changed (`:98-100`), so
 *     a deploy landing mid-run would refresh this page for a reason that has
 *     nothing to do with this behaviour. Such a run is reported red as
 *     inconclusive, never green;
 *  4. the visibility precondition. `useKeepCurrent`'s reader returns without
 *     reading on a hidden document (`useKeepCurrent.ts:46`), which would make
 *     every wait below measure the harness.
 *
 * DELIBERATELY NOT ASSERTED, named rather than silently skipped:
 *  (a) a record-level Refresh control. No console record page offers one for
 *      the record itself — the console's grammar puts Refresh on lists and on
 *      the two panels that own their own fetches, both of which are on THIS
 *      page (`SandboxSecurityPanel.tsx:74`, `SandboxDiagnosticsPanel.tsx:169`,
 *      each re-asking only its own endpoint), and record pages carry
 *      `useKeepCurrent` instead. Demanding a button for the record here would
 *      pin a shape the codebase has already answered another way;
 *  (b) the per-id read. This page answers a single-record question by scanning
 *      one listing page capped at `LOOKUP_PAGE_SIZE = 200`
 *      (`SandboxDetailPage.tsx:32`), which is exactly the backend's
 *      `_MAX_PAGE_SIZE` (`astrabox/api/routes/sandboxes.py:69`), while
 *      `GET /admin/sandboxes/{id}` exists (`routes/sandboxes.py:494-504`) and
 *      is wrapped for tests but not in `frontend/src/api.ts`. A box past row
 *      200 therefore reads as "not found" on a deployment whose list pages to
 *      it at 50 a page. Reaching that needs >200 boxes, which no lane can
 *      stage, so the per-id status is RECORDED as an annotation here and
 *      judged nowhere;
 *  (c) the foregrounded page. An operator staring at this record through the
 *      reclaim gets no return to re-read on, and whether that should become a
 *      `follow` cadence is a design decision nobody has made.
 *
 * WHY PARALLEL. It creates its own cold Agent and its own conversation-tenancy
 * box and terminates only that box — the same out-of-band reclaim two sibling
 * parallel specs already perform in this lane. Every assertion is scoped to
 * this spec's own sandbox id or to its own browser context's request log, so
 * co-running workers cannot move it; it needs no restart, no docker exec and no
 * idle timing. Conversation tenancy is required rather than incidental: the
 * campaign Agent's box is Pool-backed and shared, and terminating a shared box
 * would break the conversations co-running on it — so it is asserted, not
 * assumed.
 *
 * ENGINE. None. No model turn is sent and no engine name is read, so this is
 * correct under whichever profile the matrix selected. The one deployment
 * fixture it reads and never creates is
 * `ASTRABOX_E2E_CREDENTIAL_COLD_ENVIRONMENT`, which `createColdTestAgent` fails
 * loudly on when absent (`astraApi.ts:381-400`).
 */
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { PlatformApi } from '../fixtures/platformApi';
import { apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { returnToTab } from '../fixtures/tabReturn';

// The two localized strings this spec reads are the record's own words — the
// 'Overview' card heading and the not-found card's title. The console resolves
// language from ['localStorage','navigator'], so both are pinned, exactly as
// the sibling console specs pin them. A spec that accepted two spellings would
// accept a third.
test.use({ locale: 'en-US' });

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
// The box is already destroyed by then, so that line resolves the session and
// prints its sandbox as <unresolved>; that is this journey, not a defect.
const sessions = trackSessions();
let agentId = '';
// Registered after trackSessions so the session is deleted before its Agent.
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

/**
 * How long the control plane may take to report the new box as RUNNING.
 *
 * This is the journey's premise, not its subject: the session is already READY
 * and naming the box, so a box that is not running by now is a provisioning
 * finding and failing fast here leaves the budget for the assertions that
 * follow.
 */
const BOX_RUNNING_MS = parseTimeoutEnv('ASTRABOX_E2E_SANDBOX_RUNNING_MS', 30_000);

/** How long the backend may take to stop reporting a box it has reclaimed. */
const RECLAIM_VISIBLE_MS = parseTimeoutEnv('ASTRABOX_E2E_SANDBOX_RECLAIM_VISIBLE_MS', 60_000);

/**
 * How long a returning operator may wait for the page to answer again.
 *
 * The same knob the list spec uses, deliberately: both pages are re-read by the
 * same `useKeepCurrent`, and one mechanism should not be tuned through two
 * numbers.
 */
const REVALIDATE_MS = parseTimeoutEnv('ASTRABOX_E2E_LIST_REVALIDATE_MS', 6_000);

/** `manage:sandboxes.section_overview` and `manage:sandboxes.error_title`. */
const OVERVIEW = 'Overview';
const ERROR_TITLE = 'Could not load this sandbox';

/** The state string the product treats as live (`sandboxConfig.ts:38`). */
const LIVE_STATE = 'RUNNING';

test('the sandbox record can be re-asked while it is open, and never keeps pulsing over a box the control plane no longer runs', async ({
  page,
  request,
}) => {
  // ── observers, attached before the first navigation ───────────────────────
  // A crashed React tree also stops issuing requests and also leaves a stale
  // word on screen, so the counts below must be able to tell "never asked
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
  // document, so together they say "this page was never reloaded".
  let mainFrameNavigations = 0;
  page.on('framenavigated', (frame) => {
    if (frame === page.mainFrame()) mainFrameNavigations += 1;
  });

  // ── setup over the API: arrangement, not the journey ──────────────────────
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const runId = new Date().toISOString().replace(/[:.]/g, '-');

  const agent = await api.createColdTestAgent(`__e2e_sandbox_record_freshness_${runId}`);
  agentId = String(agent.agent_id || '');
  expect(agentId, 'conversation-tenancy Agent created').not.toEqual('');
  expect(
    String(agent.sandbox_id || '').trim(),
    'conversation-tenancy Agent must not own a shared sandbox: this spec destroys the box '
      + 'it opens, and a shared one belongs to the conversations co-running on it',
  ).toEqual('');

  const created = await api.startConversation(agentId);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  const ready = await api.waitForSessionReady(sessionId);
  const sandboxId = String(ready.sandbox_id || '');
  expect(sandboxId, 'a READY session names the box it claimed').not.toEqual('');
  test.info().annotations.push({ type: 'e2e_sandbox_id', description: sandboxId });

  /**
   * What the deployment says about this box, through the same listing the
   * record page reads — `null` when the listing does not carry it at all.
   */
  const listedState = async (): Promise<string | null> => {
    const listing = await platform.listSandboxes();
    const row = (listing.items ?? []).find(
      (item) => String(item.sandbox_id || '') === sandboxId,
    );
    return row ? String(row.state || '') : null;
  };

  // The premise: the control plane runs this box. Everything below is about a
  // page that outlives that fact, so it has to be a fact first.
  //
  // The reading is kept as it is taken, not re-read afterwards: the label the
  // page must match has to be the SAME reading that satisfied this poll, or a
  // state that moved between two reads would be charged to the console.
  let liveState = '';
  await expect
    .poll(
      async () => {
        const state = await listedState();
        liveState = state ?? '';
        if (state === null) return '<absent>';
        return state.trim().toUpperCase() || '<empty>';
      },
      {
        timeout: BOX_RUNNING_MS,
        intervals: [500, 1_000, 2_000],
        message:
          `the deployment never reported sandbox ${sandboxId} as ${LIVE_STATE} even though its `
          + 'session is READY and names it. That is a provisioning finding, not a console one, '
          + 'and nothing below can be judged while the box was never live.',
      },
    )
    .toBe(LIVE_STATE);

  // Pin the console language before the FIRST navigation, or the two strings
  // this spec reads come back in whatever the runner's locale happens to be.
  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* localStorage unavailable — the en-US navigator locale still applies */
    }
  });

  // ── the record, opened directly ───────────────────────────────────────────
  // A deep link, deliberately not a click through /manage/sandboxes: that list
  // has a reader of its own and its own spec, and arriving from it would leave
  // a reader of this one unable to say which surface refreshed what. This is
  // the last navigation this spec performs.
  const recordRead = apiPath('/admin/sandboxes');
  await page.goto(appPath(`/manage/sandboxes/${sandboxId}`));

  const epoch = `sandbox-record-${runId}`;
  await page.evaluate((value: string) => {
    (window as unknown as { __e2eRecordPageEpoch?: string }).__e2eRecordPageEpoch = value;
  }, epoch);
  const navigationsAtArrival = mainFrameNavigations;

  // One pill on this route: `SandboxDetailPage` renders exactly one through
  // ConsoleRecordPage, and the manage shell renders none. If a future shell
  // adds one, scope this to the heading row rather than loosening the match.
  const pill = page.getByTestId('status-pill');
  const pulsing = page.locator('[data-testid="status-pill"][data-pulse="true"]');
  const overview = page
    .locator('[data-slot="card"]')
    .filter({ has: page.getByRole('heading', { name: OVERVIEW, exact: true }) });

  // This is the record of THIS box, not of a neighbour that happens to share a
  // prefix: the Overview card prints the id whole.
  await expect(
    overview.getByText(sandboxId, { exact: true }),
    `the record at /manage/sandboxes/${sandboxId} must name that box`,
  ).toBeVisible();

  // ── BEFORE: the record claims the box is alive, and it is ─────────────────
  await expect(pill, 'the record carries exactly one status pill').toHaveCount(1);
  await expect(
    pill,
    'the record must label the box with the backend\'s own state string; the page renders '
      + '`sandboxStateLabel`, which never translates or normalizes it',
  ).toHaveText(liveState);
  await expect(
    pill,
    'a RUNNING box reads on the astra tone — the same token the list row uses, so the two '
      + 'surfaces cannot drift in what live looks like',
  ).toHaveAttribute('data-tone', 'astra');
  await expect(
    pill,
    'the record must be caught making the liveness claim while the box is live. Without '
      + 'this the verdict below is satisfied by a page that never rendered at all',
  ).toHaveAttribute('data-pulse', 'true');

  // Prove the meter is armed before any low reading from it is believed. The
  // page must have read the listing to render the record at all, so a zero here
  // means this spec is counting the wrong path — most likely the console's API
  // base having diverged from ASTRABOX_E2E_APP_PREFIX — and an unarmed counter
  // reports perfect silence forever.
  expect(
    hits(recordRead),
    `no GET ${recordRead} was seen while the record rendered, so this spec is not measuring `
      + 'anything. Check the app prefix before reading any count below as news.',
  ).toBeGreaterThanOrEqual(1);

  // The reader that keeps this page current returns without reading on a hidden
  // document (`useKeepCurrent.ts:46`). Asserted here so a hidden page fails as a
  // hidden page instead of timing out below and reading as a product defect.
  expect(
    await page.evaluate(() => document.visibilityState),
    'the record page must be visible: the reader that keeps it current does nothing on a '
      + 'hidden document, and every wait below would then be measuring the harness',
  ).toEqual('visible');

  // The page's own last read, for the throttle the return has to clear. Taken
  // after the record has rendered, so it is never earlier than the read
  // `useKeepCurrent` throttles against.
  const readAt = Date.now();

  // ── the world changes while the tab sits there ────────────────────────────
  const reclaim = await api.terminateSandbox(sessionId);
  expect(reclaim.session_id, 'the reclaim receipt must name this conversation').toEqual(sessionId);
  expect(reclaim.status, 'the box must actually have been reclaimed').toEqual('sandbox-reclaimed');
  expect(
    reclaim.sandbox_id,
    'the reclaim must have taken the box this record describes',
  ).toEqual(sandboxId);
  // `killed` is deliberately not asserted: it is tenancy-dependent and gives
  // opposite honest answers depending on who else was in the box.

  // ── the oracle: the deployment's own answer has changed ───────────────────
  await expect
    .poll(async () => (await listedState())?.trim().toUpperCase() ?? '<absent>', {
      timeout: RECLAIM_VISIBLE_MS,
      intervals: [500, 1_000, 2_000],
      message:
        `the backend still reports sandbox ${sandboxId} as ${LIVE_STATE} after reclaiming it. `
        + 'This is a reclaim question, not a console one — nothing below can be judged until '
        + 'the deployment itself stops saying the box is running.',
    })
    .not.toBe(LIVE_STATE);

  // Recorded, never judged: the per-id route the record page does not use. A
  // reader of a failure should be able to see what the control plane said about
  // the box by id at this moment without this spec pinning a fix shape.
  test.info().annotations.push({
    type: 'e2e_sandbox_describe_status',
    description: String(await platform.status('GET', `/admin/sandboxes/${sandboxId}`)),
  });

  // ── the operator returns, and the record must re-ask ──────────────────────
  const readsBeforeReturn = hits(recordRead);
  await returnToTab(page, readAt);

  await expect
    .poll(() => hits(recordRead), {
      timeout: REVALIDATE_MS,
      intervals: [250, 500, 1_000],
      message:
        `the record page issued no further GET ${recordRead} after the operator came back to `
        + 'the tab, so it is still describing the deployment as it was when the page was '
        + 'opened. The return is supposed to reach `useKeepCurrent(load)` at '
        + 'SandboxDetailPage.tsx:73, which re-reads on `visibilitychange` and `focus` once '
        + 'its 5s throttle has passed — and the return above waited that out, so a red here '
        + 'is the page, not the timing.',
    })
    .toBeGreaterThan(readsBeforeReturn);

  // Integrity before journey: a run whose tab was replaced by a release landing
  // mid-test learned nothing about freshness either way, and the reload would
  // have re-read the record for the wrong reason.
  expect(
    await page.evaluate(
      () => (window as unknown as { __e2eRecordPageEpoch?: string }).__e2eRecordPageEpoch ?? null,
    ),
    'the document this journey started in must be the one that finished it. A lost epoch '
      + 'means the tab was reloaded — exactly the help this spec must not have had — and the '
      + 'one thing that can reload a tab by itself is the frontend release guard '
      + '(frontendRelease.ts:117-118), which listens on the same two events dispatched here. Read '
      + 'this run as inconclusive, not as evidence either way.',
  ).toEqual(epoch);
  expect(
    mainFrameNavigations - navigationsAtArrival,
    'no navigation of any kind may follow the record opening: this spec calls no '
      + 'page.reload() and no second goto, so a green verdict cannot have come from one',
  ).toEqual(0);

  // ── the verdict: the liveness claim is dropped ────────────────────────────
  await expect
    .poll(() => pulsing.count(), {
      timeout: REVALIDATE_MS,
      intervals: [250, 500, 1_000],
      message:
        `the deployment has stopped reporting sandbox ${sandboxId} as ${LIVE_STATE}, but the `
        + 'record still shows a breathing status pill over it. A pulse is a statement about '
        + 'now, and an operator reading it will act on a box that is gone.',
    })
    .toBe(0);

  // ── and the record says what the deployment says ──────────────────────────
  // Both sides are read on every tick rather than the branch being decided from
  // one snapshot: a reclaim can pass through a settled state on its way out of
  // the listing, and a page compared against a reading taken at a different
  // instant would be charged for the deployment moving. The failure message
  // carries both sides, so a red names which one is wrong.
  const AGREES = 'the record agrees with the deployment';
  let settledState: string | null = null;
  await expect
    .poll(
      async () => {
        try {
          const state = await listedState();
          settledState = state;
          const labels = (await pill.allTextContents()).map((text) => text.trim());
          const notFound = await page.locator('[data-slot="error-state"]').count();
          if (state === null) {
            return labels.length === 0 && notFound === 1
              ? AGREES
              : `the deployment no longer lists the box; the record shows `
                + `${labels.length} pill(s) [${labels.join(', ')}] and ${notFound} not-found card(s)`;
          }
          return labels.length === 1 && labels[0] === state.trim()
            ? AGREES
            : `the deployment says "${state}"; the record shows ${labels.length} pill(s) `
              + `[${labels.join(', ')}]`;
        } catch (error) {
          // "We could not read the page" and "the page is wrong" send a reader
          // to different places, so a mid-render read is reported as itself and
          // retried rather than failing the poll outright.
          return `reading the record failed: ${String(error).slice(0, 160)}`;
        }
      },
      {
        timeout: REVALIDATE_MS,
        intervals: [250, 500, 1_000],
        message:
          'the record must end up saying what the deployment says about this box: the '
          + 'not-found card for a box the listing no longer carries (the page has that '
          + 'branch and this is what it is for), otherwise the backend\'s own state string '
          + '— equality, because "no longer RUNNING" would also be satisfied by the page '
          + 'printing something else entirely.',
      },
    )
    .toBe(AGREES);
  test.info().annotations.push({
    type: 'e2e_sandbox_state_after_reclaim',
    description: settledState === null ? '<absent from listing>' : String(settledState),
  });

  if (settledState === null) {
    // `manage:sandboxes.not_found` through ConsoleErrorState, which names
    // itself `data-slot="error-state"` and carries role="alert". Asserted for
    // the operator's half of it: the card is on screen above, and this is the
    // sentence they read.
    await expect(
      page.getByText(ERROR_TITLE, { exact: true }),
      'the not-found card must say which failure this is, in the operator\'s language',
    ).toBeVisible();
  }

  // A record that agreed with the control plane inside a crashed tree would
  // still read as agreement.
  expect(
    uncaught,
    `uncaught exception while the record page followed its box:\n${uncaught.join('\n')}`,
  ).toEqual([]);

  await test.info().attach('sandbox-record-freshness', {
    body: JSON.stringify(
      {
        sandbox_id: sandboxId,
        session_id: sessionId,
        state_while_live: liveState,
        state_after_reclaim: settledState,
        record_reads: { before_return: readsBeforeReturn, total: hits(recordRead) },
        main_frame_navigations: mainFrameNavigations - navigationsAtArrival,
      },
      null,
      2,
    ),
    contentType: 'application/json',
  });
});
