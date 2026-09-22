/**
 * E2E: the operator reading an incident is reading the session, not a snapshot.
 *
 * The journey is the one /manage/errors exists for. An operator opens the error
 * list during a live incident, clicks the row, and reads the session's record
 * while the conversation keeps running underneath them. They never reload. What
 * the record says has to stay true, or it has to say when it was taken.
 *
 * TWO HALVES, AND ONLY ONE OF THEM PASSES ON THIS TREE.
 *
 * The first half is a regression guard and is expected GREEN.
 * `SessionDetailPage` re-reads through `useKeepCurrent(load, { follow: ... })`
 * (SessionDetailPage.tsx:116-118) while the record is NOT in
 * `RESTING_SESSION_STATES` (`{READY, TERMINATED, DELETED}`,
 * SessionDetailPage.tsx:77). A reclaimed conversation derives
 * RECOVERY_REQUIRED (`_sanitize_session`, session_service.py:398-403: READY +
 * `runtime_unavailable` => RECOVERY_REQUIRED), which is not resting, so the
 * page follows the recovery on its own and must show the session healing.
 *
 * The second half is the finding, and is expected RED. The moment the session
 * heals, the record reads READY — and READY is `RESTING`, so `follow` goes
 * false and the interval is cleared. READY is also the state the row holds for
 * the WHOLE of a running turn: `bridge_loop.py:747` never writes BUSY to
 * the session row. So the record stops reading its session exactly when the
 * session is busiest, and the only other readers `useKeepCurrent` has are
 * `focus` and `visibilitychange` (useKeepCurrent.ts:60-67), which a reader
 * sitting on the page does not fire. A second incident landing thirty seconds
 * later is invisible for as long as the tab stays open, and the page offers no
 * way to find out: no ConsoleRecordPage consumer carries a Refresh control, and
 * none prints when its read was taken. An operator cannot tell a five-second-old
 * view from a fifty-minute-old one.
 *
 * So the verdict accepts EITHER legitimate answer — follow, or disclose — and
 * fails only when the page does neither. The disclosure branch pins an
 * interface that does not exist yet: the console's own `common:refresh`
 * aria-label ("Refresh", used by every LIST page, e.g. ErrorsListPage.tsx:220)
 * and its own `manage:system.captured_at` sentence ("Captured {{at}}", whose
 * one caller is SystemPage.tsx). A product change that words the capture line
 * differently moves this locator with it — the wording here is this spec's
 * guess, not an agreed contract.
 *
 * WHAT THE INCIDENTS ARE, AND WHY THEY ARE SHAPED THIS WAY.
 *
 * Incident 1 is a product action: POST /sessions/{id}/sandbox/terminate
 * reclaims the compute without ending the conversation
 * (`_terminate_session_direct`, session_kernel/workers/lifecycle/commands.py:439-465)
 * — state READY, `runtime_unavailable` true, `sandbox_id` cleared,
 * `last_error` cleared. It derives RECOVERY_REQUIRED on every read and it puts
 * the session on /manage/errors (`_summarize_session_error`,
 * admin_service.py:782-783 fires on `last_error` OR `runtime_unavailable`).
 *
 * The one synthetic write in the setup gives that incident words. Terminate
 * deliberately clears `last_error`, and `admin_list_errors` summarises the RAW
 * row (admin_service.py:825-838), so the Message column falls back to
 * `state` — the bare word "READY" (admin_service.py:797), which cannot pick one
 * row out of many. `patchSessionDoc(last_error)` writes the field the
 * death-convergence writer sets (`terminal_session_updates`,
 * sandbox_lifecycle.py:185-218, `last_error="sandbox terminated"`).
 *
 * Incident 2 is `{runtime_unavailable: true, last_error: <marker>}` with the
 * binding LEFT INTACT. That is not an invented state: it is exactly what
 * `runtime_ensure` writes when a turn cannot attach its transport to a box that
 * is still there (runtime_ensure.py:770 and :790). Leaving `sandbox_id` in
 * place is deliberate — clearing it would orphan a live Pod that teardown has
 * no way to reclaim, and a leaked box on this lane is paid for by whichever
 * spec runs next on a full disk.
 *
 * WHAT THIS SPEC DOES NOT ASSERT, AND WHY.
 *
 * Not `current_turn_id`. The record's "Current turn" comes from
 * `admin_get_session_trace`, which reads it off the SESSIONS row
 * (admin_service.py:1053) — and the codebase states that this field "is a
 * terminal-time mirror that is empty during a live turn"
 * (session_kernel/workers/turn/worker.py:337-341); the snapshot owns the live
 * slot. A gate on it would never go green and would read as this page's fault.
 * That mismatch is a real reporting defect, but it is a different one.
 *
 * Not "the turn is still running" at verdict time. Turn length is the model's,
 * and this spec reads no model output at all. The counting prompt below is
 * sized to outlive the verdict window, but the defect does not depend on it:
 * the page stops following at READY whether or not a turn is in flight.
 *
 * KNOWN FLAKE MODE, stated rather than hidden: if the turn settles during the
 * verdict window, its terminal writers can overwrite `last_error` and erase the
 * second incident from the row. The verdict re-reads the API on every tick and
 * reports that case as `incident-gone`, which is a finding about the fixture,
 * not about the console. If it recurs, interrupt the turn before writing
 * incident 2 and accept that the scene is then a settled conversation.
 *
 * WHY EXCLUSIVE. It pays two cold sandbox provisions inside the 180s wall — the
 * first claim and the re-borrow after the reclaim — which is capacity, not
 * isolation. It does not need the one-worker serial group: the only box it
 * destroys is its own conversation-tenancy box.
 *
 * ENGINE-AGNOSTIC. Nothing here reads a reply. The turn is needed only to make
 * the platform re-borrow a box and clear the incident, which every engine
 * profile does at dispatch. `createColdTestAgent` clones the matrix-selected
 * Agent's deployment-proven model onto its own conversation-tenancy Agent, so
 * the spec runs under whichever `ASTRABOX_E2E_AGENT_NAME` the lane chose. It
 * reads the deployment fixture `ASTRABOX_E2E_CREDENTIAL_COLD_ENVIRONMENT` and
 * fails loudly when it is absent; it never creates or rewrites it.
 */
import { expect, test, type Locator, type Page } from '@playwright/test';

import { AstraApi, type AdminSessionRecord } from '../fixtures/astraApi';
import { patchSessionDoc } from '../fixtures/dbOracle';
import { apiPath, appPath, parseTimeoutEnv } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

/**
 * How long the healed session may take to reach the API after the turn is
 * admitted — one cold re-borrow of a reclaimed conversation's box.
 *
 * Deliberately not generous. The steps before it can spend ~90s of the lane's
 * 180s wall, so this is sized to leave a readable assertion failure instead of
 * a process-group kill. Raising it converts a red that names its cause into a
 * run that reads as "the spec hung"; if a loaded node really provisions slower
 * than this, lower it and re-scope the spec.
 */
const HEAL_MS = parseTimeoutEnv('ASTRABOX_E2E_INCIDENT_HEAL_MS', 75_000);

/**
 * How long a record page may take to catch up with its own session.
 *
 * Twice the 10s cadence of the console's slowest live surface (SystemPage), and
 * ten times `FOLLOW_INTERVAL_MS` (useKeepCurrent.ts:15), so a loaded host is not
 * charged for a slow tick. Both verdicts below use it, so the pass and the fail
 * are measured against the same window.
 */
const FRESHNESS_MS = parseTimeoutEnv('ASTRABOX_E2E_RECORD_FRESHNESS_MS', 20_000);

/**
 * How long the record may take to prove it is reading at all, at the incident.
 *
 * Seven ticks of the 2s follower. This runs while the record is
 * RECOVERY_REQUIRED, where `follow` is true — so a red here is a reader that is
 * gone or a tree that crashed, and it lands in seconds rather than after the
 * provisioning waits below.
 */
const MECHANISM_MS = parseTimeoutEnv('ASTRABOX_E2E_RECORD_MECHANISM_MS', 15_000);

/** How long the interrupted turn's own request is given to unwind at the tail. */
const STREAM_DRAIN_MS = 5_000;

// The localized strings this spec reads, pinned. `manage:session_state.*`,
// `manage:sessions.*`, `manage:system.captured_at` and `common:refresh` in
// frontend/src/i18n/locales/en/. The pill carries no `data-state` on a record
// page (ConsoleRecordPage.tsx:52-57 passes only tone and label), so the label
// is the only handle on it and the language has to be nailed down.
const PILL_RECOVERY = 'Recovery required';
const PILL_READY = 'Ready';
const CARD_OVERVIEW = 'Overview';
const FACT_SANDBOX = 'Sandbox';
const COL_MESSAGE = 'Message';
const REFRESH = 'Refresh';
/** `manage:system.captured_at` is "Captured {{at}}" — match the stem only. */
const CAPTURED_AT = /^Captured /;
/** What a `ConsoleFact` shows in place of a value it does not have. */
const NO_VALUE = '—';

// One localized surface with no machine-readable state on it (see above): pin
// the runner locale AND the persisted language the app's own switch writes. A
// spec that accepted two spellings would accept a third.
test.use({ locale: 'en-US' });

// Sessions created here are deleted only when the test passes. A failure keeps
// the box and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();
let agentId = '';
// Registered after the tracker, so the session is removed before the Agent it
// ran on — the order a-sandbox-record-reports-the-box-not-an-empty-card uses.
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

/** One console card, addressed by the heading it puts on itself. */
function card(page: Page, heading: string): Locator {
  return page
    .locator('[data-slot="card"]')
    .filter({ has: page.getByRole('heading', { name: heading, exact: true }) });
}

test('an incident record follows its session while the turn runs, or says when it was read', async ({
  page,
  request,
}) => {
  // ── observers, attached before the first navigation ───────────────────────
  // A crashed React tree also stops issuing requests and also leaves a stale
  // word on screen. A listener added after the render that mattered cannot tell
  // those apart from a page that simply stopped asking.
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
  // document, so together they say "this verdict was taken on an untouched
  // page".
  let mainFrameNavigations = 0;
  page.on('framenavigated', (frame) => {
    if (frame === page.mainFrame()) mainFrameNavigations += 1;
  });

  // ── setup over the API: arrangement, not the journey ──────────────────────
  const api = new AstraApi(request);
  const runId = `${Date.now()}-${test.info().workerIndex}`;
  const agent = await api.createColdTestAgent(`__e2e_incident_record_${runId}`);
  agentId = String(agent.agent_id || '');
  expect(agentId, 'conversation-tenancy Agent created').not.toEqual('');
  expect(
    String(agent.sandbox_id || '').trim(),
    'conversation tenancy: the Agent must not own a shared box, or the reclaim below '
      + 'would take a box a sibling conversation is sitting on',
  ).toEqual('');

  const created = await api.startConversation(agentId);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  const ready = await api.waitForSessionReady(sessionId);
  const firstSandboxId = String(ready.sandbox_id || '').trim();
  expect(firstSandboxId, 'a READY conversation names the box it claimed').not.toEqual('');

  // ── incident 1: a product action, not an injected fault ───────────────────
  const reclaim = await api.terminateSandbox(sessionId);
  expect(
    String(reclaim.status || ''),
    'terminate reclaims the compute and leaves the conversation alive',
  ).toEqual('sandbox-reclaimed');

  // The incident's words. The single synthetic write in this spec, and the
  // field the death-convergence writer sets — see the header for why terminate
  // leaves it empty and why an empty one cannot be found on the list.
  const marker = `E2E-INCIDENT-${runId}`;
  const patched = patchSessionDoc(sessionId, { last_error: marker });
  expect(patched, 'exactly one session row carries this conversation').toHaveLength(1);
  expect(
    Boolean(patched[0].runtime_unavailable),
    'the reclaim must have left the conversation degraded before this write; without '
      + '`runtime_unavailable` the row derives READY and there is no incident to read',
  ).toBe(true);

  // The page is never accused of a state the deployment had not yet written.
  const incident = await api.waitForAdminSession(
    sessionId,
    (s) =>
      String(s.state || '') === 'RECOVERY_REQUIRED'
      && String(s.last_error || '').includes(marker),
    60_000,
  );
  expect(String(incident.sandbox_id || '').trim(), 'a reclaimed conversation holds no box').toEqual('');

  // Pin the console language before the FIRST navigation, or every label below
  // reads whatever the runner's locale happens to be (see test.use above).
  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* localStorage unavailable — the en-US navigator locale still applies */
    }
  });

  // ── the hand-off: the incident surface gives up the record ────────────────
  await page.goto(appPath('/manage/errors'));

  // Addressed as the element it is: the table is laid out as CSS grid, which is
  // why console-interaction.audit and a-sandbox-record address rows this way
  // too. Deliberately NOT through ConsoleSearch — it does not render below
  // SCANNABLE_ROWS (ConsoleControls.tsx:40), so on a quiet host a spec that
  // typed into it would be green because the control was absent.
  const row = page
    .locator('tr[tabindex], [role="row"][tabindex]')
    .filter({ hasText: sessionId.slice(0, 8) });
  await expect(
    row,
    `exactly one error row must name session ${sessionId}. The page asks for 500 rows and `
      + 'the server clamps to a 500-row recency window with no paging, so a conversation '
      + 'seconds old sorts to the top; check GET /api/v1/admin/errors before reading a miss '
      + 'as a routing defect.',
  ).toHaveCount(1);
  await expect(
    row,
    `the row must carry the incident's own words, not the fallback ${COL_MESSAGE} the `
      + 'summariser prints when `last_error` is empty (the bare row state)',
  ).toContainText(marker);

  await row.click();
  const recordPath = appPath(`/manage/sessions/${sessionId}`);
  await expect(
    page,
    'the incident surface must hand the operator the record of the session it named',
  ).toHaveURL((url) => url.pathname === recordPath);

  const epoch = `incident-record-${runId}`;
  await page.evaluate((value: string) => {
    (window as unknown as { __e2eRecordPageEpoch?: string }).__e2eRecordPageEpoch = value;
  }, epoch);
  const navigationsAtArrival = mainFrameNavigations;

  // ── the scene, as the operator finds it ───────────────────────────────────
  // The pill is the h1's literal adjacent sibling (ConsoleRecordPage.tsx:52-57),
  // which keeps every other pill on the shell out of this locator.
  const pill = page.locator('h1 + [data-testid="status-pill"]');
  const lastError = card(page, CARD_OVERVIEW).locator('p[data-slot="verbatim"]');
  /** A rail fact's value — the quiet label's own next sibling (`ConsoleFact`). */
  const railFact = (label: string) =>
    page.getByText(label, { exact: true }).locator('xpath=following-sibling::div[1]');

  await expect(pill, 'the record of a reclaimed conversation reads Recovery required').toHaveText(
    PILL_RECOVERY,
    { timeout: 30_000 },
  );
  await expect(pill, 'and wears the tone that says it needs attention').toHaveAttribute(
    'data-tone',
    'crimson',
  );
  await expect(
    lastError,
    'the record must quote the incident the error row was pointing at, verbatim',
  ).toHaveText(marker);
  await expect(
    railFact(FACT_SANDBOX),
    'a reclaimed conversation names no box',
  ).toHaveText(NO_VALUE);

  // Prove the meter is armed before any low reading from it is believed. The
  // page must have read the record to render it at all, so a zero here means
  // this spec is counting the wrong path — most likely the console's API base
  // having diverged from ASTRABOX_E2E_APP_PREFIX — and an unarmed counter
  // reports perfect silence forever.
  const detailPath = apiPath(`/admin/sessions/${sessionId}/detail`);
  const tracePath = apiPath(`/admin/sessions/${sessionId}/trace`);
  expect(
    hits(detailPath),
    `no GET ${detailPath} was seen while the record rendered, so this spec is not measuring `
      + 'anything. Check the app prefix before reading any count below as news.',
  ).toBeGreaterThanOrEqual(1);

  // The follower reads only while the document is visible (useKeepCurrent.ts:46).
  // Asserted here so a hidden page fails as a hidden page instead of timing out
  // below and reading as a product defect.
  expect(
    await page.evaluate(() => document.visibilityState),
    'the record page must be visible: the reader that keeps it current returns without '
      + 'reading on a hidden document, and every wait below would then be measuring the '
      + 'harness rather than the product',
  ).toEqual('visible');

  // ── mechanism: at RECOVERY_REQUIRED the page really is reading ────────────
  // A delta, never a total. This is the control for the silence asserted later:
  // it establishes that the reader exists, that the tree is alive, and that the
  // counter sees its requests — so a later count that does not move is the
  // `follow` predicate and not a dead page.
  const readsAtIncident = hits(detailPath);
  await expect
    .poll(() => hits(detailPath), {
      timeout: MECHANISM_MS,
      intervals: [500, 500, 1_000],
      message:
        `the record issued no further GET ${detailPath} while the session was `
        + 'RECOVERY_REQUIRED. That state is not in RESTING_SESSION_STATES '
        + '(SessionDetailPage.tsx:77), so `useKeepCurrent(load, { follow })` '
        + '(SessionDetailPage.tsx:116-118) should be re-reading every 2s. Either that '
        + 'reader is gone, or the tree crashed — check the pageerror list at the tail.',
    })
    .toBeGreaterThan(readsAtIncident);

  // ── the conversation goes on under the operator ──────────────────────────
  // Started and never awaited: this spec reads nothing the model says, and
  // waiting for the body would hand the whole budget to the turn. From this
  // line to the last verdict the spec issues no reload, no goto, no click and
  // no keypress.
  const streaming = api
    .streamPrompt(sessionId, 'Count slowly from 1 to 60, one number per line.')
    .catch(() => undefined);

  // One read carries every fact the record is then required to show. Dispatch
  // re-borrows a box and clears the degradation (`runtime_ensure.py:884 / :920`,
  // inside `contextlib.suppress`) — so a red here is the backend, not the
  // console, and the assertion says so.
  const healed = await api.waitForAdminSession(
    sessionId,
    (s) =>
      String(s.state || '') === 'READY'
      && !String(s.last_error || '').trim()
      && !!String(s.sandbox_id || '').trim(),
    HEAL_MS,
  );
  const healedSandboxId = String(healed.sandbox_id || '').trim();
  expect(
    healedSandboxId,
    'the page cannot be accused of not following until the deployment has moved. The turn '
      + 'did not clear the incident: the dispatch write-back (runtime_ensure.py:884 / :920) '
      + 'never landed, or the re-borrow failed — a different defect from this one.',
  ).not.toEqual(firstSandboxId);
  test.info().annotations.push({ type: 'e2e_healed_sandbox_id', description: healedSandboxId });

  // ── half one: the record followed the recovery, with no help ─────────────
  await expect
    .poll(async () => ((await pill.textContent()) || '').trim(), {
      timeout: FRESHNESS_MS,
      intervals: [500, 1_000, 1_000],
      message:
        'the session recovered while this record was open and the operator did nothing. '
        + `The pill never reached "${PILL_READY}". The API says READY with sandbox `
        + `${healedSandboxId}; if the pill is still "${PILL_RECOVERY}", the follower at `
        + 'SessionDetailPage.tsx:116-118 stopped reading a record that was still in motion.',
    })
    .toBe(PILL_READY);
  await expect(pill, 'a recovered conversation reads settled, not degraded').toHaveAttribute(
    'data-tone',
    'mint',
  );
  await expect(
    lastError,
    'the incident is over, so the record must stop quoting it — the Last error row is '
      + 'rendered only while `detail.last_error` is set (SessionDetailPage.tsx:256-261)',
  ).toHaveCount(0);
  await expect(
    railFact(FACT_SANDBOX),
    'the record must name the box the platform says this conversation now holds — equality, '
      + 'because "no longer —" would also be satisfied by the page printing anything at all',
  ).toHaveText(healedSandboxId);

  const readsAtReady = hits(detailPath);
  const tracesAtReady = hits(tracePath);

  // ── incident 2: the same session goes wrong again, under the same eyes ────
  // The binding is left intact on purpose: see the header. This is the shape
  // `runtime_ensure` writes when a turn cannot attach to a box that is still
  // there (runtime_ensure.py:770, :790).
  const secondMarker = `E2E-INCIDENT-AGAIN-${runId}`;
  const patchedAgain = patchSessionDoc(sessionId, {
    runtime_unavailable: true,
    last_error: secondMarker,
  });
  expect(patchedAgain, 'exactly one session row carries this conversation').toHaveLength(1);
  expect(
    String(patchedAgain[0].sandbox_id || '').trim(),
    'the second incident must be written onto the healed binding, or the box it names is '
      + 'orphaned and teardown can no longer reclaim it',
  ).toEqual(healedSandboxId);

  const reincident = await api.waitForAdminSession(
    sessionId,
    (s) =>
      String(s.state || '') === 'RECOVERY_REQUIRED'
      && String(s.last_error || '').includes(secondMarker),
    30_000,
  );
  expect(
    String(reincident.state || ''),
    'the deployment must be reporting the second incident before the page is judged on it',
  ).toEqual('RECOVERY_REQUIRED');

  // ── the verdict: follow, or say when you were read ───────────────────────
  // Both legitimate answers are accepted, and the API is re-read on every tick
  // so a red names which half moved. `incident-gone` is the fixture's own flake
  // (see the header), not a statement about the console.
  const apiTrail: string[] = [];
  const noteApi = async (): Promise<boolean> => {
    let seen: string;
    let stillDegraded = false;
    try {
      const now: AdminSessionRecord = await api.adminSessionDetail(sessionId);
      stillDegraded = String(now.last_error || '').includes(secondMarker);
      seen = `${String(now.state || '<none>')} last_error=${
        String(now.last_error || '').slice(0, 60) || '<empty>'
      }`;
    } catch (error) {
      // "We could not ask" and "it moved" send a reader to different places.
      seen = `<read failed: ${String(error).slice(0, 120)}>`;
    }
    if (apiTrail[apiTrail.length - 1] !== seen) {
      apiTrail.push(seen);
      test.info().annotations.push({ type: 'e2e_session_after_reincident', description: seen });
    }
    return stillDegraded;
  };

  const followed = async (): Promise<boolean> =>
    ((await pill.textContent()) || '').trim() === PILL_RECOVERY
    && (await pill.getAttribute('data-tone')) === 'crimson'
    && (await card(page, CARD_OVERVIEW).getByText(secondMarker, { exact: false }).count()) > 0;

  const capturedAt = page.getByText(CAPTURED_AT);
  const refresh = page.getByRole('button', { name: REFRESH });
  const disclosed = async (): Promise<boolean> =>
    (await capturedAt.count()) > 0 && (await refresh.count()) > 0;

  // Held rather than re-derived: the assertion after the poll must judge the
  // SAME evaluation the poll ended on. Asking again would sample a different
  // moment and could contradict the tick that just passed.
  let lastVerdict = 'stale';
  const verdict = async (): Promise<string> => {
    const stillDegraded = await noteApi();
    if (await followed()) lastVerdict = 'followed';
    else if (await disclosed()) lastVerdict = 'disclosed';
    else lastVerdict = stillDegraded ? 'stale' : 'incident-gone';
    return lastVerdict;
  };

  await expect
    .poll(verdict, {
      timeout: FRESHNESS_MS,
      intervals: [500, 1_000, 1_000],
      message:
        'a second incident landed on this session while the operator sat on its record, and '
        + `the record neither followed it nor admitted it might be old.\n`
        + `  API now: RECOVERY_REQUIRED carrying "${secondMarker}".\n`
        + `  Page: pill still reads "${PILL_READY}", with no "${secondMarker}" on it.\n`
        + `  Reads of ${detailPath} since the page reached Ready: counted at the tail `
        + '(expected 0 today).\n'
        + '  CAUSE: READY is in RESTING_SESSION_STATES (SessionDetailPage.tsx:77), so '
        + '`follow` goes false and useKeepCurrent clears its interval (useKeepCurrent.ts:73-78). '
        + 'READY is also what the row reads for the whole of a running turn '
        + '(bridge_loop.py:747), so this record stops reading its session exactly while '
        + 'the session is busy.\n'
        + '  EITHER ANSWER PASSES: keep following a session that is not at rest, OR print '
        + `when the record was read ("${CAPTURED_AT.source}", manage:system.captured_at) and `
        + `give it a "${REFRESH}" control (common:refresh — every list page already has one, `
        + 'no record page does).\n'
        + '  A verdict of `incident-gone` instead means the turn settled and its terminal '
        + 'writers erased the marker from the row — that is this spec\'s fixture, not the '
        + 'console; see the header.',
    })
    .not.toBe('stale');

  // `not.toBe('stale')` also admits `incident-gone`, which is not a pass — it
  // is this spec's own fixture having been overwritten. Refused separately so
  // it can never be read as the console behaving.
  expect(
    lastVerdict,
    'the second incident disappeared from the session row before the record could be '
      + `judged on it. The turn settled inside the verdict window and its terminal writers `
      + 'overwrote `last_error`; the e2e_session_after_reincident annotations carry the '
      + 'trail. This says nothing about the console — re-run, or interrupt the turn before '
      + 'writing incident 2 (see the header).',
  ).not.toBe('incident-gone');

  // If the page took the disclosure branch, the control it offers has to work:
  // saying "this is old" and then refusing to re-ask is worse than silence.
  if (lastVerdict === 'disclosed') {
    await expect(capturedAt, 'the record states the moment it was read').toBeVisible();
    await refresh.click();
    await expect(
      pill,
      'a Refresh the operator pressed must re-ask the session, not redraw the answer it had',
    ).toHaveText(PILL_RECOVERY, { timeout: FRESHNESS_MS });
    await expect(pill).toHaveAttribute('data-tone', 'crimson');
    await expect(
      card(page, CARD_OVERVIEW).getByText(secondMarker, { exact: false }),
      'and the re-read must carry the incident the API is reporting now',
    ).toBeVisible();
  }

  // ── negative control: none of this was bought with a refresh ─────────────
  expect(
    await page.evaluate(
      () => (window as unknown as { __e2eRecordPageEpoch?: string }).__e2eRecordPageEpoch ?? null,
    ),
    'the document this journey started in must be the one that finished it. A lost epoch '
      + 'means the tab was reloaded, which is exactly the help this spec must not have had. '
      + 'The one thing in the app that can reload a tab by itself is the frontend release '
      + 'guard (frontendRelease.ts) — a deploy landing mid-run would show up here.',
  ).toEqual(epoch);
  expect(
    mainFrameNavigations - navigationsAtArrival,
    'no navigation of any kind may follow the arrival on the record: the spec calls no '
      + 'page.reload() and no second goto, so a green result cannot have come from a refresh',
  ).toEqual(0);
  await expect(page, 'the verdict was taken on the record, not somewhere else').toHaveURL(
    (url) => url.pathname === recordPath,
  );

  // A record that followed inside a crashed tree would not have followed at all.
  expect(
    uncaught,
    `uncaught exception while the operator read the incident record:\n${uncaught.join('\n')}`,
  ).toEqual([]);

  await test.info().attach('incident-record-freshness', {
    body: JSON.stringify(
      {
        sessionId,
        agentId,
        firstSandboxId,
        healedSandboxId,
        marker,
        secondMarker,
        detailPath,
        tracePath,
        recordReads: {
          atIncident: readsAtIncident,
          atReady: readsAtReady,
          atVerdict: hits(detailPath),
          sinceReady: hits(detailPath) - readsAtReady,
        },
        traceReads: {
          atReady: tracesAtReady,
          atVerdict: hits(tracePath),
          sinceReady: hits(tracePath) - tracesAtReady,
        },
        apiTrailAfterReincident: apiTrail,
        mainFrameNavigationsAfterArrival: mainFrameNavigations - navigationsAtArrival,
        budgetsMs: { heal: HEAL_MS, freshness: FRESHNESS_MS, mechanism: MECHANISM_MS },
      },
      null,
      2,
    ),
    contentType: 'application/json',
  });

  // ── tail: leave the conversation in a state teardown can take offline ────
  // `_assert_conversation_safe_to_take_offline` refuses to delete a session
  // while a turn is active (lifecycle/commands.py:502-530), and the tracker's
  // delete runs on the passing path. The verdict is already taken, so a noisy
  // teardown must not be able to change the result — hence the catches.
  await api.interruptSession(sessionId).catch(() => undefined);
  // Bounded on purpose. A stream that ignores the interrupt must not be able to
  // convert a verdict that has already been taken into a process-group kill,
  // which would report as "the spec hung" and keep the scene for no reason.
  await Promise.race([
    streaming,
    new Promise<void>((resolve) => setTimeout(resolve, STREAM_DRAIN_MS)),
  ]);
});
