/**
 * E2E: a background result still reaches its conversation when fifty newer
 * manifests were opened while it waited.
 *
 * A user asks their agent to do something in the background, the agent launches
 * a background subagent and answers immediately, and the user goes elsewhere.
 * While that manifest sits open, the rest of the deployment keeps launching and
 * settling background work of its own. The user comes back expecting the answer.
 *
 * THE MECHANISM THIS IS ABOUT. One loop carries background results back: the
 * reconcile tick calls `_materialize_background_continuations_once` (bootstrap
 * .py:208), which reads the fifty NEWEST opened manifests deployment-wide —
 * `find({channel, event_type}).sort([(occurred_at,-1),(event_seq,-1)]).limit(50)`
 * (background_continuation.py:136-159). Nothing in that query asks whether a
 * manifest is still waiting, `session_events` is append-only and never pruned,
 * and `limit=50` is not configurable (bootstrap.py:208 passes nothing). So the
 * window is a work queue that finished history fills, and a manifest that falls
 * out of it is never looked at again — not on a reload, not on a new turn, not
 * on a restart. From the user's seat the header keeps reading "Running in
 * background" forever, because `_get_background_task_state` reports OPEN for an
 * opened event with no materialized counterpart (background_continuation.py:89-
 * 134) and `session_read.py:1038` maps that to BACKGROUND_RUNNING.
 *
 * WHAT IS COMPRESSED, AND HOW HONESTLY. "Other conversations launch background
 * work" is fifty-five seeded SETTLED manifests, not fifty-five live
 * conversations — see `seedSettledBackgroundManifests` in fixtures/dbOracle.ts
 * for what a seeded pair is and why it is faithful in the only two properties
 * the sweep reads. What it does not reproduce is fifty-five organic launches; a
 * sweep re-keyed off something other than `occurred_at` would need the seeding
 * to follow.
 *
 * WHY EXCLUSIVE, AND WHY IT MUST BE SERIAL. `_list_background_task_opened_events`
 * has no session predicate. While the backlog is in place EVERY session's
 * background continuation is starved, not only this one, so this file belongs in
 * suite-contract.json `playwright.exclusive.serial_files` (1 worker). A reviewer
 * who leaves it out of that list silently breaks other specs' background work,
 * and the failure is reported against whichever of them noticed first.
 *
 * ONE DELIBERATE BREAK WITH THE TEARDOWN CONVENTION. The seeded rows are removed
 * in `afterEach` UNCONDITIONALLY, not through `onPassOnly`, with a leftover
 * sweep at the top of the test as the second line of defence. `trackSessions`
 * still keeps the conversation on failure, which is the scene; keeping the
 * backlog would turn every later background spec red for the wrong reason.
 *
 * ENGINE. `run_in_background` on the Agent tool is Claude Code's own vocabulary
 * and the launch prompt is written in it, exactly as the sibling background
 * specs are — the browser lane's only engine input is ASTRABOX_E2E_AGENT_NAME,
 * and the per-profile launch wording (`instructions.controllable_child`) lives
 * in a matrix file this lane is never handed. Scheduled under another profile
 * the launch simply does not happen, and the run ends at the `insist` above
 * with "no turn.background_tasks_opened was journalled" — an unmet precondition,
 * not a statement about the sweep. What IS portable is the verdict:
 * nothing here compares an engine status to 'completed' or an engine kind to
 * 'claude_code', because agent-engine-matrix.json settles a deepseek_harness
 * child as `inactive`/`running` with reason `completed`. The engine's own
 * strings are only ever carried into a diagnostic.
 */
import { test, expect, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import {
  SEEDED_MANIFEST_TAG_PREFIX,
  newestOpenedManifestWindow,
  removeSeededBackgroundManifests,
  seedSettledBackgroundManifests,
  sessionEvents,
  type OpenedManifestWindowRow,
  type SeededBackgroundManifests,
} from '../fixtures/dbOracle';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { insist } from '../fixtures/insist';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

// One asserted string is localized — the header's "Running in background"
// (chat:status.background_running). The console detects language as
// ['localStorage','navigator'], so an unpinned runner locale decides which
// spelling it gets, and a spec that accepted two spellings would also accept a
// third nobody wrote it against. Pin it the way
// agent-per-session-sandbox.exclusive.spec.ts:33 does.
test.use({ locale: 'en-US' });
const BACKGROUND_RUNNING_LABEL = 'Running in background';

// The event vocabulary this spec reads, spelled as the journal stores it.
const OPENED_EVENT_TYPE = 'turn.background_tasks_opened';
const MATERIALIZED_EVENT_TYPE = 'turn.background_tasks_materialized';
const ENGINE_MESSAGE_EVENT_TYPE = 'engine.message';

// The sweep's window, as the product pins it: `_materialize_background_
// continuations_once(limit=50)` (background_continuation.py:225) and a caller
// that passes nothing (bootstrap.py:208). Not a knob — a larger number moves
// the cliff to a longer history rather than removing it, because the window is
// ordered by recency and carries no predicate on whether a manifest is waiting.
const BACKGROUND_WINDOW_LIMIT = 50;
// Five more than the window, so the conversation's manifest is out of it even
// if some seeded rows end up below another deployment-wide launch.
const SEEDED_MANIFESTS = 55;

const READY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_READY_TIMEOUT_MS', 60_000);
// How long the child holds its manifest at the `tasks_still_pending` gate
// (background_continuation.py:464). It is the burial window: the seeding has to
// land while the manifest is still open, and a lost race raises this knob.
const CHILD_SLEEP_MS = parseTimeoutEnv('ASTRABOX_E2E_BGWINDOW_CHILD_SLEEP_MS', 25_000);
// First evidence that the model actually backgrounded an Agent.
const OPENED_PROBE_MS = parseTimeoutEnv('ASTRABOX_E2E_BGWINDOW_OPENED_PROBE_MS', 60_000);
// The control's own wait: the child settling into durable engine evidence.
const CHILD_SETTLE_MS = parseTimeoutEnv('ASTRABOX_E2E_BGWINDOW_CHILD_SETTLE_MS', 45_000);
// The verdict's wait. Six ticks of the reconcile loop, which sleeps
// ASTRABOX_RECONCILE_SCAN_INTERVAL_S between passes (bootstrap.py:216) and
// defaults to 10s (reconcile_worker.py:52). A deployment configured with a
// longer interval needs this raised to match.
const SETTLE_MS = parseTimeoutEnv('ASTRABOX_E2E_BGWINDOW_SETTLE_MS', 60_000);

// Polling the document store costs a `docker exec psql` per sample; one second
// apart is plenty against a loop that ticks every ten.
const DB_POLL_INTERVALS = [1_000];

/** Launch exactly one background Agent, reply immediately, never wait. */
function backgroundLaunchPrompt(options: {
  runId: number;
  marker: string;
  parentMarker: string;
  sleepSeconds: number;
}): string {
  return [
    `E2E background window ${options.runId}.`,
    'This is a product regression test. Follow the tool instructions literally.',
    'Task 1: launch one Agent tool call with run_in_background=true.',
    "The background Agent task must use Bash to run exactly: python3 - <<'PY'",
    'import time',
    `time.sleep(${options.sleepSeconds})`,
    `print("${options.marker}")`,
    'PY',
    `After the Bash command completes, the background Agent final summary must contain ${options.marker}.`,
    'In the parent turn, do not wait for the background Agent to finish.',
    `Immediately after launching the background Agent call, reply with one short sentence containing ${options.parentMarker}.`,
    'The parent turn must not use Bash directly.',
  ].join('\n');
}

function eventType(event: Record<string, unknown>): string {
  return String(event.event_type || '').trim();
}

function eventPayload(event: Record<string, unknown>): Record<string, unknown> {
  const payload = event.payload;
  return payload && typeof payload === 'object' ? (payload as Record<string, unknown>) : {};
}

/** The conversation's own opened manifest — the one the backlog has to bury. */
function openedManifest(sessionId: string): Record<string, unknown> | null {
  return sessionEvents(sessionId).find((event) => eventType(event) === OPENED_EVENT_TYPE) ?? null;
}

/** The counterpart the sweep writes, matched the way the sweep matches it. */
function materializedFor(sessionId: string, openedSeq: number): Record<string, unknown> | null {
  return sessionEvents(sessionId).find((event) => (
    eventType(event) === MATERIALIZED_EVENT_TYPE
    && Number(eventPayload(event).source_opened_event_seq || 0) === openedSeq
  )) ?? null;
}

/**
 * The durable engine evidence the sweep reads, asked for directly.
 *
 * `_collect_background_continuation_projection` (background_continuation.py:352)
 * projects a settled child from the `engine.message` rows it reads through
 * `_load_background_engine_messages` (:491), so a row carrying the child's
 * marker means the child finished AND the material the sweep needs is on disk. Deliberately not `listChildRuns`: the child-run view consumes
 * `turn.background_tasks_materialized` alongside live engine frames
 * (session_child_run_view.py:29,342,370), so what it shows could be a
 * consequence of the defect rather than an independent fact about the child.
 */
function childEvidenceIsDurable(sessionId: string, marker: string): boolean {
  return sessionEvents(sessionId).some((event) => (
    eventType(event) === ENGINE_MESSAGE_EVENT_TYPE
    && JSON.stringify(eventPayload(event)).includes(marker)
  ));
}

interface HeaderClaim {
  pulse: string;
  claimsBackground: boolean;
}

/**
 * What the conversation header is telling the user right now.
 *
 * A pill detaching mid re-render is not an answer, so it reads as `<reading>`
 * and the poll asks again rather than failing on the re-render.
 */
async function headerClaim(page: Page): Promise<HeaderClaim> {
  const pill = page.getByTestId('run-view').getByTestId('status-pill').first();
  try {
    if (await pill.count() === 0) return { pulse: '<no header>', claimsBackground: false };
    const [pulse, label] = await Promise.all([pill.getAttribute('data-pulse'), pill.innerText()]);
    return {
      pulse: String(pulse ?? '<unset>'),
      claimsBackground: label.replace(/\s+/g, ' ').includes(BACKGROUND_RUNNING_LABEL),
    };
  } catch {
    return { pulse: '<reading>', claimsBackground: false };
  }
}

// ── Teardown ──────────────────────────────────────────────────────────────
// Registered BEFORE `trackSessions()` on purpose: afterEach hooks run in
// registration order, so the scene is attached and the backlog removed while
// the conversation the tracker may delete still answers.
//
// The tag is recorded BEFORE the seeding runs, not after it returns: an insert
// that commits and then fails its read-back would otherwise leave a backlog
// nothing knows the name of, and that backlog starves every later spec.
interface BackgroundWindowScene {
  sessionId: string;
  tag: string;
  openedSeq: number;
  marker: string;
  seeded: SeededBackgroundManifests | null;
}

let scene: BackgroundWindowScene | null = null;

test.afterEach(async ({ request }, testInfo) => {
  const current = scene;
  scene = null;
  if (!current) return;
  if (testInfo.status !== testInfo.expectedStatus) {
    const api = new AstraApi(request);
    const [session, childRuns, messages] = await Promise.all([
      api.getSession(current.sessionId).catch((error: unknown) => ({ error: String(error) })),
      api.listChildRuns(current.sessionId).catch((error: unknown) => ({ error: String(error) })),
      api.getMessages(current.sessionId, 50).catch((error: unknown) => ({ error: String(error) })),
    ]);
    await testInfo.attach('background-window-scene.json', {
      contentType: 'application/json',
      body: JSON.stringify({
        session_id: current.sessionId,
        opened_event_seq: current.openedSeq,
        child_marker: current.marker,
        seed_tag: current.tag,
        seeded: current.seeded,
        session,
        child_runs: childRuns,
        messages,
        session_events: sessionEvents(current.sessionId),
        newest_opened_manifest_window: newestOpenedManifestWindow(BACKGROUND_WINDOW_LIMIT),
      }, null, 2),
    });
  }
  // Unconditional, and by tag rather than by what the seeding reported, so a
  // seeding that committed and then threw is still cleaned up.
  const removed = removeSeededBackgroundManifests({ tag: current.tag });
  // eslint-disable-next-line no-console -- the report tail is where an operator looks
  console.log(`background-window backlog removed: tag=${current.tag} rows=${removed}`);
});

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

test('a background result reaches its conversation after fifty newer manifests were opened', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const marker = `BGWIN_${runId}_DONE`;
  const parentMarker = `PARENT_LAUNCHED_${runId}`;
  const tag = `${SEEDED_MANIFEST_TAG_PREFIX}${runId}`;

  // A worker killed mid-test leaves its backlog behind, and a backlog outlives
  // the run that wrote it. Sweep before seeding rather than in `beforeAll`,
  // where an unresolvable database container would skip the whole file — and a
  // runtime skip ends this lane exactly as a failure does.
  const leftovers = removeSeededBackgroundManifests({ prefix: SEEDED_MANIFEST_TAG_PREFIX });
  if (leftovers > 0) {
    // eslint-disable-next-line no-console -- the report tail is where an operator looks
    console.log(`background-window: swept ${leftovers} seeded row(s) left by an earlier run`);
  }

  // ── Arrangement stays on the API: a user picks a conversation, they do not
  //    author one to ask for background work. ──────────────────────────────
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  const current: BackgroundWindowScene = { sessionId, tag, openedSeq: 0, marker, seeded: null };
  scene = current;
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  await api.waitForSessionReady(sessionId, READY_TIMEOUT_MS);
  // Explicit, so the child's one Bash call runs unattended instead of resting
  // on whatever default the conversation was created with.
  await api.setPermissionMode(sessionId, 'bypassPermissions');

  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* localStorage unavailable — the en-US navigator locale still applies */
    }
  });
  await openSessionView(page, sessionId);

  // ── The user asks for background work, through the real composer. ────────
  // `parentMarker` is the prompt's way of making the parent answer instead of
  // waiting on its child; it is deliberately not asserted. What proves the
  // launch is the journalled manifest below, and asserting the reply as well
  // would spend a poll on the model's wording.
  const prompt = backgroundLaunchPrompt({
    runId,
    marker,
    parentMarker,
    sleepSeconds: Math.max(1, Math.round(CHILD_SLEEP_MS / 1_000)),
  });
  await sendPrompt(page, sessionId, prompt);

  // ── The manifest has to exist before anything can bury it. ───────────────
  // Asking again beats skipping: a `test.skip()` here ends the round exactly as
  // a failure does, and it would be the model's choice that decided it rather
  // than the platform's behaviour.
  const opened = await insist<Record<string, unknown>>({
    ask: async (attempt) => {
      if (attempt > 1) await api.postTurnInput(sessionId, prompt);
    },
    probe: async () => {
      const deadline = Date.now() + OPENED_PROBE_MS;
      for (;;) {
        const event = openedManifest(sessionId);
        if (event) return event;
        if (Date.now() >= deadline) return null;
        await page.waitForTimeout(500);
      }
    },
    what:
      `no ${OPENED_EVENT_TYPE} was journalled within ${OPENED_PROBE_MS}ms: the model `
      + 'answered inline instead of launching a background Agent, so there is no open '
      + 'manifest to bury and nothing about the sweep\'s window can be observed',
    budgetMs: OPENED_PROBE_MS * 2,
    probeMs: OPENED_PROBE_MS,
  });
  const openedSeq = Number(opened.event_seq || 0);
  const openedAt = String(opened.occurred_at || '');
  expect(openedSeq, 'the opened manifest must carry its journal sequence').toBeGreaterThan(0);
  expect(openedAt, 'the opened manifest must carry its timestamp').not.toEqual('');
  current.openedSeq = openedSeq;

  // ── Bury it: fifty-five settled manifests, every one of them newer. ──────
  const seeded = seedSettledBackgroundManifests({
    count: SEEDED_MANIFESTS,
    afterOccurredAt: openedAt,
    tag,
  });
  current.seeded = seeded;
  expect(seeded.rows, 'each seeded manifest is an opened row and its counterpart')
    .toBe(SEEDED_MANIFESTS * 2);

  // ── PRECONDITIONS. Neither is the verdict; both fail loud, because a scene
  //    that never formed must not be read as a product answer. ─────────────
  expect(
    materializedFor(sessionId, openedSeq),
    `the burial lost the race with the child: this manifest was already materialized `
      + `before the backlog landed, so the sweep's window was never the thing under test. `
      + `Raise ASTRABOX_E2E_BGWINDOW_CHILD_SLEEP_MS (currently ${CHILD_SLEEP_MS}) and run again.`,
  ).toBeNull();

  const openedWindow: OpenedManifestWindowRow[] = newestOpenedManifestWindow(BACKGROUND_WINDOW_LIMIT);
  expect(
    openedWindow.length,
    `fewer than ${BACKGROUND_WINDOW_LIMIT} opened manifests exist deployment-wide, so nothing `
      + 'was buried; the seeding did not land',
  ).toBe(BACKGROUND_WINDOW_LIMIT);
  expect(
    openedWindow.some((row) => row.session_id === sessionId),
    `this conversation's manifest is still inside the sweep's ${BACKGROUND_WINDOW_LIMIT}-row `
      + 'window, so the run proves nothing about a manifest that falls out of it; '
      + `window tail=${JSON.stringify(openedWindow.slice(-3))}`,
  ).toBe(false);

  // ── The user goes elsewhere: a real page switch to the console list. ─────
  await page.goto(appPath('/manage/sessions'), { waitUntil: 'domcontentloaded' });
  await expect(page).toHaveURL((url) => url.pathname === appPath('/manage/sessions'));
  await expect(
    page.getByTestId('run-view'),
    'leaving the conversation must tear its view down, or the user never left',
  ).toHaveCount(0);

  // ── CONTROL. The child finished and the evidence the sweep reads is on
  //    disk. Present here plus a manifest still open is starvation; absent
  //    here is a child that never settled, and this run exercised nothing. ──
  await expect
    .poll(() => childEvidenceIsDurable(sessionId, marker), {
      timeout: CHILD_SETTLE_MS,
      intervals: DB_POLL_INTERVALS,
      message:
        `no durable ${ENGINE_MESSAGE_EVENT_TYPE} for ${sessionId} carries ${marker} within `
        + `${CHILD_SETTLE_MS}ms. The background child never settled, so this run did not `
        + 'exercise a starved window and a red below would be misattributed to the platform.',
    })
    .toBe(true);

  // ── The user comes back. A reload is the read path anyone checking on a
  //    background result actually takes, and it rebuilds the page from the
  //    Session resource rather than from a live invalidation. ──────────────
  await openSessionView(page, sessionId);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 60_000 });

  // ── VERDICT. Driven by the real `_reconcile_loop`, not by a `docker exec` of
  //    the pass: with fifty-five settled manifests seeded newer, the real loop
  //    genuinely cannot reach this one, so no parameter is altered and nothing
  //    races the driver. ────────────────────────────────────────────────────
  await expect
    .poll(async () => {
      const session = await api.getSession(sessionId);
      const backgroundTaskState = session.background_task_state;
      const header = await headerClaim(page);
      return {
        result_materialized: materializedFor(sessionId, openedSeq) !== null,
        session_state: String(session.state || ''),
        background_task_open: Boolean(
          backgroundTaskState
          && typeof backgroundTaskState === 'object'
          && String((backgroundTaskState as Record<string, unknown>).state || '') === 'OPEN',
        ),
        header_pulse: header.pulse,
        header_claims_background: header.claimsBackground,
      };
    }, {
      timeout: SETTLE_MS,
      intervals: DB_POLL_INTERVALS,
      message:
        'a background result must reach its conversation even when fifty newer manifests were '
        + 'opened while it waited. The sweep carries only the fifty newest opened manifests '
        + 'deployment-wide, with no predicate on whether they are still waiting '
        + '(background_continuation.py:136-159), so this one is never looked at again and the '
        + 'header goes on claiming work is in flight.',
    })
    .toEqual({
      result_materialized: true,
      session_state: 'READY',
      background_task_open: false,
      header_pulse: 'false',
      header_claims_background: false,
    });

  // A background result that arrives as a failure is the same broken screen
  // from the user's seat, and this keeps a crashed turn from being read as
  // "the result came back".
  await expect(
    page.getByTestId('assistant-message')
      .filter({ hasText: /API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i }),
    'no error bubble may stand in for the background result',
  ).toHaveCount(0);
});
