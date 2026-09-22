/**
 * E2E: the turn that launched a background Agent is lost after the launch and
 * before its terminal, and the conversation still reports the child.
 *
 * THE JOURNEY. A user asks for work that the engine backgrounds. The child is
 * launched, the box is fine, the vendor CLI is fine, the child is already
 * writing files. What goes away is only the host's turn worker — the process
 * holding the launcher turn — between the `async_launched` receipt and the
 * turn's terminal. The user does nothing more: they are owed a result, and the
 * only thing standing between them and it is whether the platform wrote down
 * that a child was open.
 *
 * WHY THAT WINDOW IS THE WHOLE STORY. The manifest that says "this turn left a
 * child running" lives in memory for the whole turn — `state.background_tasks_opened`
 * (`workers/turn/state.py:52`), set from the adapter's envelope at
 * `workers/turn/bridge_loop.py:805-813` — and is journaled exactly once, at
 * `workers/turn/bridge_terminal.py:605-606`, behind
 * `not turn_failed and session_state == READY`. For Claude Code the adapter
 * does not even build it until the Result: `build_background_task_manifest` is
 * called at `engine/claude_code_client.py:621` (and :1295) on the `ResultMessage`
 * branch, so there is a real interval in which the child is running and nothing
 * durable knows it. Lose the worker inside that interval and
 * `turn.background_tasks_opened` is never written; `_get_background_task_state`
 * (`service_mixins/background_continuation.py:89-134`) reads only that event, so
 * the console's "background work is running" (`SessionPage.tsx:503`
 * `backgroundTasksPending`) has nothing to read, and the continuation that
 * materializes the child's answer into the transcript has no manifest to
 * materialize.
 *
 * WHAT IS BEING ASSERTED. Not a route. The contract is: a child this platform
 * launched is tracked, closed out, and its result reaches the conversation with
 * no further action from the user. Which writer keeps that promise — the
 * launcher's own terminal, the recovery lane, or the resident reader — is the
 * runtime's business, and this spec is deliberately blind to it. That is why
 * the launcher's `last_turn_status` is printed and never asserted: the
 * transcript-recovery lane settles provisionally and then projects from the
 * mirror, so pinning a value would assert an implementation route instead of
 * the contract.
 *
 * WHY A FRAME HOLD AND AN EVICTION. The window is otherwise a few hundred
 * milliseconds wide. `hold_after_frame` on `tool-output-available`
 * (`astrabox/testing/e2e_faults.py:310-361`, reached from
 * `workers/turn/bridge_loop.py:889`) parks the real worker right after the
 * launch receipt — for a backgrounded Agent call that receipt is the tool
 * result Claude Code answers with (`engine/frame_translator.py:33`,
 * ToolResultBlock -> tool-output-available). `adminEvictRuntime` then takes the
 * host's handle and only that: `_disconnect_evicted_runtime`
 * (`runtime_manager.py:1026-1037`) cancels the runtime task and closes the turn
 * client, and the assertions below require the sandbox to still be running, so
 * the child keeps working in a live box that nothing on the host is reading.
 *
 * ENGINE: Claude Code. Both halves of this journey are its vocabulary —
 * `run_in_background` and the `async_launched` receipt, and
 * `build_background_task_manifest`, which exists only in
 * `engine/claude_code_background.py`. The requirement is read where the engine
 * actually lives (the Agent's Environment, not the Agent: `AgentRecord`
 * carries `environment_name` and no engine, `api/routes/agents.py:36-78`) and
 * refused by name, not by a runtime `test.skip()` — a runtime skip SIGTERMs the
 * whole worker. There is no per-spec engine registry to exempt it from:
 * agent-engine-matrix.json carries profiles and no per-spec rows, and
 * suite-contract.json's `model_conditional_skips` is the frozen set of files
 * whose source calls `test.skip(` (tests/e2e_suite_membership_contract_test.py
 * :372-401), which this one does not. Whichever profile `--research-agent`
 * selects is what this file runs against; on a non-Claude campaign it reports
 * the mis-selection at the named assertion below.
 *
 * DEPLOYMENT PREREQUISITES, read and never created here: `ASTRABOX_E2E_FAULTS=1`
 * on the server, and `ASTRABOX_E2E_TURN_TERMINAL_DROP_FAULT_FILE` naming the
 * host path both the server and this runner can write. Absent either, the
 * barrier is never consumed and the anti-vacuity poll below fails by name
 * rather than passing on an ordinary turn.
 *
 * FALSE-RED GUARDS, in assertion order. (1) If the child died with the host
 * runtime, its DONE sentinel never appears and the spec reports THAT — a
 * different story, told loudly — before it reports anything about tracking.
 * (2) If the launcher turn never settles after the release, the settle poll
 * fails with its own message. (3) If the model answered inline instead of
 * backgrounding, `insist` re-asks in the same conversation and then fails as an
 * unmet precondition. (4) If the model reached for some other tool first, the
 * hold lands on that frame, the child never starts, and the launch probe says
 * so with the consumed-fault state in its message.
 *
 * WHY EXCLUSIVE: it arms a file-driven fault hook on the shared backend host,
 * parks a real bridge worker, and evicts a host runtime. It does NOT restart
 * the server container, so it stays out of run-round.mjs's serial restart pass.
 *
 * NOT COVERED, and no hook invented for it: the sibling half where the vendor's
 * own Result reports `is_error` with the CLI still alive, which trips
 * `turn_failed=True` at the same gate. Interrupt cannot provoke it (cancelled
 * settles COMPLETED and the manifest IS recorded), killing the box kills the
 * child, and egress denial on this deployment also cuts the transcript mirror.
 * Closing it needs a counted fault at the existing `turn_frame_processed` seam.
 */
import { expect, test } from '@playwright/test';

import { AstraApi, type ChildRunRecord, type SessionRecord } from '../fixtures/astraApi';
import { oracleDbPath, sessionEvents } from '../fixtures/dbOracle';
import { parseTimeoutEnv, refuseIfNotTheDeployment } from '../fixtures/env';
import {
  FRAME_HOLD_FAULT_BASE,
  armFrameHoldFault,
  clearFrameHoldFault,
  frameHoldConsumed,
  frameHoldFaultPath,
  readFrameHoldFault,
  releaseFrameHoldFault,
} from '../fixtures/frameHold';
import { insist } from '../fixtures/insist';
import { PlatformApi } from '../fixtures/platformApi';
import { requireSandboxHandle, sandboxRunning } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

// Per-step ceilings, not expectations. The lane's wall is the runner's hard cut
// (suite-contract max_test_seconds), and a spec never states its own budget —
// these only decide which named assertion reports a stall.
const READY_MS = parseTimeoutEnv('ASTRABOX_E2E_BG_LOST_READY_MS', 90_000);
// Reaching the launch: the barrier consuming this session's declaration AND the
// child's own first write landing in the workspace.
const LAUNCH_MS = parseTimeoutEnv('ASTRABOX_E2E_BG_LOST_LAUNCH_MS', 90_000);
// The launcher turn settling after the eviction and the release — through
// whichever lane the runtime picks.
const SETTLE_MS = parseTimeoutEnv('ASTRABOX_E2E_BG_LOST_SETTLE_MS', 90_000);
// How long the platform may take to declare that it is tracking the child.
const TRACK_MS = parseTimeoutEnv('ASTRABOX_E2E_BG_LOST_TRACK_MS', 90_000);
// How long the child's completion may take to reach the conversation.
const MATERIALIZE_MS = parseTimeoutEnv('ASTRABOX_E2E_BG_LOST_MATERIALIZE_MS', 120_000);

// Long enough that the launcher turn is certainly lost while the child is still
// working, short enough to leave the materialization room inside the wall.
const CHILD_SLEEP_SECONDS = Number.parseInt(
  process.env.ASTRABOX_E2E_BG_LOST_CHILD_SLEEP_SECONDS || '20',
  10,
);

// The frame the barrier parks on. For a backgrounded Agent call this is the
// CLI's own `async_launched` tool result — the first tool output the prompt
// below permits the parent to produce.
const HELD_FRAME_TYPE = 'tool-output-available';

const TERMINAL_SESSION_STATES = new Set([
  'RECOVERY_REQUIRED',
  'TERMINATED',
  'DELETED',
  'FAILED',
]);

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

function eventPayload(event: Record<string, unknown>): Record<string, unknown> {
  return event.payload && typeof event.payload === 'object' && !Array.isArray(event.payload)
    ? (event.payload as Record<string, unknown>)
    : {};
}

function eventTypes(events: Record<string, unknown>[]): string[] {
  return events.map((event) => String(event.event_type || '')).filter(Boolean);
}

function openedManifests(events: Record<string, unknown>[]): Record<string, unknown>[] {
  return events.filter(
    (event) => String(event.event_type || '') === 'turn.background_tasks_opened',
  );
}

function materializationsForOpenedEvent(
  events: Record<string, unknown>[],
  openedEventSeq: number,
): Record<string, unknown>[] {
  return events.filter((event) => (
    String(event.event_type || '') === 'turn.background_tasks_materialized'
    && Number(eventPayload(event).source_opened_event_seq) === openedEventSeq
  ));
}

/** The session's background window as an object, or null when it holds none. */
function backgroundWindow(session: SessionRecord): Record<string, unknown> | null {
  const value = session.background_task_state;
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/**
 * Launch one detached child that leaves two marks in the workspace.
 *
 * STARTED proves the child is really running while the host loses the turn;
 * DONE proves it finished its work in the live box. Without the second one,
 * "the user never heard about it" would be a statement about nothing.
 */
function backgroundAgentPrompt(
  runId: number,
  marker: string,
  startedPath: string,
  donePath: string,
): string {
  return [
    `E2E background launcher loss ${runId}. Follow every instruction exactly.`,
    'Launch exactly one Agent tool call with run_in_background=true and subagent_type=general-purpose.',
    'That background Agent must call Bash exactly once, running this complete command:',
    "python3 - <<'PY'",
    'from pathlib import Path',
    'import time',
    `Path(${JSON.stringify(startedPath)}).write_text('started')`,
    `time.sleep(${CHILD_SLEEP_SECONDS})`,
    `Path(${JSON.stringify(donePath)}).write_text('done')`,
    `print('${marker}_DONE')`,
    'PY',
    `After that Bash call succeeds, the background Agent's final answer must contain ${marker}_DONE.`,
    'The parent turn must not call Bash, TaskOutput, wait, or any other tool.',
    `As soon as the Agent tool returns its background task id, reply with one short sentence containing PARENT_LAUNCHED_${runId}.`,
  ].join('\n');
}

interface TrackingObservation {
  openedEvents: Record<string, unknown>[];
  sawOpenWindow: boolean;
  lastSession: SessionRecord | null;
  lastEventTypes: string[];
}

/**
 * Watch for the platform's own declaration that it is tracking the child.
 *
 * Records rather than asserts, so the anti-vacuity check (the child really
 * finished) can be asserted FIRST and a dead child cannot be reported as a
 * tracking failure. Returns as soon as both halves are seen — the journal
 * manifest and the OPEN window the console reads — so in the healthy case this
 * costs a poll interval rather than its ceiling.
 */
async function observeBackgroundTracking(
  api: AstraApi,
  sessionId: string,
  timeoutMs: number,
): Promise<TrackingObservation> {
  const deadline = Date.now() + timeoutMs;
  const observation: TrackingObservation = {
    openedEvents: [],
    sawOpenWindow: false,
    lastSession: null,
    lastEventTypes: [],
  };
  while (Date.now() < deadline) {
    const session = await api.getSession(sessionId);
    const events = sessionEvents(sessionId);
    observation.lastSession = session;
    observation.lastEventTypes = eventTypes(events);
    observation.openedEvents = openedManifests(events);
    if (String(backgroundWindow(session)?.state || '') === 'OPEN') {
      observation.sawOpenWindow = true;
    }
    if (observation.openedEvents.length > 0 && observation.sawOpenWindow) {
      return observation;
    }
    await sleep(2_000);
  }
  return observation;
}

/** Is this workspace file readable with this exact content? */
async function sentinelReadable(
  api: AstraApi,
  platform: PlatformApi,
  sessionId: string,
  path: string,
  expected: string,
): Promise<boolean> {
  const name = path.split('/').at(-1);
  const listing = await platform.listFiles(sessionId, '/workspace', 15_000);
  if (!listing.entries?.some((entry) => entry.name === name && entry.kind === 'file')) {
    return false;
  }
  return (await api.downloadFileText(sessionId, path, 15_000)).trim() === expected;
}

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene — session, journal, sandbox Pod — and names it in the report tail.
const sessions = trackSessions();

let openSessionId = '';
onPassOnly(async ({ request }) => {
  // A launcher whose worker was taken may leave the engine mid-flight; only a
  // passing run may clear it, so a failure keeps every live handle.
  if (openSessionId) await new AstraApi(request).interruptSession(openSessionId);
});

test('a background launcher lost before its terminal still tracks and materializes its child', async ({
  page,
  request,
}) => {
  // Before anything is measured: prove this product is behind the page. A dev
  // server proxying elsewhere answers every walk plausibly and wrongly.
  await refuseIfNotTheDeployment(async (url, headers) => ({
    status: (await request.get(url, { failOnStatusCode: false, headers })).status(),
  }));

  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const runId = Date.now();
  const marker = `BG_LOST_${runId}`;
  const startedPath = `/workspace/.astrabox-e2e-bg-lost-${runId}.started`;
  const donePath = `/workspace/.astrabox-e2e-bg-lost-${runId}.done`;

  expect(
    Number.isSafeInteger(CHILD_SLEEP_SECONDS) && CHILD_SLEEP_SECONDS > 0,
    'ASTRABOX_E2E_BG_LOST_CHILD_SLEEP_SECONDS must be a positive whole number of seconds',
  ).toBe(true);

  // The journal pair (opened / materialized) is the durable half of the
  // contract; name the oracle before creating anything.
  test.info().annotations.push({ type: 'oracle-db', description: oracleDbPath() });
  test.info().annotations.push({ type: 'e2e_fault_base', description: FRAME_HOLD_FAULT_BASE });

  // ── ENGINE. Read where the engine lives: on the Agent's Environment. ──────
  const agent = await api.defaultAgent();
  const environmentName = String(agent.environment_name || '').trim();
  const environments = await platform.listEnvironments();
  const environment = environments.find((row) => String(row.name || '') === environmentName);
  expect(
    environment,
    `the selected Agent ${JSON.stringify(String(agent.name || ''))} names Environment `
      + `${JSON.stringify(environmentName)}, which this deployment does not have`,
  ).toBeTruthy();
  expect(
    String(environment?.engine_kind || ''),
    'this journey exists only on the engine that emits background manifests: '
      + '`build_background_task_manifest` lives in engine/claude_code_background.py and no '
      + 'other adapter emits BackgroundTasksOpened. Select a Claude Code profile for this '
      + 'lane (--research-agent / ASTRABOX_E2E_AGENT_NAME), or leave this file out of the '
      + 'selection on a campaign that runs another engine.',
  ).toEqual('claude_code');

  // ── The conversation the user is having. ─────────────────────────────────
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  openSessionId = sessionId;

  const ready = await api.waitForSessionReady(sessionId, READY_MS);
  await api.setPermissionMode(sessionId, 'bypassPermissions');
  const sandboxId = String(ready.sandbox_id || '').trim();
  expect(
    sandboxId,
    'this journey needs the box the platform published: the eviction below must be '
      + 'provably narrower than losing the sandbox',
  ).not.toBe('');

  // Armed BEFORE the first prompt: the barrier parks the worker on the launch
  // receipt, and a declaration written after the send would race it.
  const frameHoldPath = frameHoldFaultPath(sessionId);
  armFrameHoldFault(frameHoldPath, sessionId, HELD_FRAME_TYPE);
  test.info().annotations.push({ type: 'e2e_turn_frame_hold_file', description: frameHoldPath });

  let launcherTurnId = '';
  let settledLastTurnStatus = '';
  let tracking: TrackingObservation = {
    openedEvents: [],
    sawOpenWindow: false,
    lastSession: null,
    lastEventTypes: [],
  };

  try {
    // The user watches their own conversation for the whole outage.
    await page.setViewportSize({ width: 1280, height: 720 });
    await openSessionView(page, sessionId);

    // ── The send is a real one, from the composer. ─────────────────────────
    const prompt = backgroundAgentPrompt(runId, marker, startedPath, donePath);
    await sendPrompt(page, sessionId, prompt);
    await expect(
      page.getByTestId('user-message').last(),
      'the user bubble must render — proof the launcher was dispatched from the page',
    ).toContainText(marker, { timeout: 30_000 });

    // ── Reach the window: the worker parked on the launch receipt, and a
    //    child already running behind it. Ask again rather than skip — a
    //    runtime skip ends the round exactly as a failure does, and decides
    //    coverage by the model's mood. ────────────────────────────────────────
    await insist<true>({
      ask: async (attempt) => {
        if (attempt > 1) await api.postTurnInput(sessionId, prompt);
      },
      probe: async () => {
        const deadline = Date.now() + LAUNCH_MS;
        while (Date.now() < deadline) {
          const session = await api.getSession(sessionId);
          if (TERMINAL_SESSION_STATES.has(String(session.state || ''))) {
            throw new Error(
              `session ${sessionId} reached ${session.state} while waiting for the launch; `
                + `last_error=${session.last_error || '<none>'}`,
            );
          }
          const held = frameHoldConsumed(frameHoldPath, sessionId, HELD_FRAME_TYPE);
          if (held && await sentinelReadable(api, platform, sessionId, startedPath, 'started')) {
            return true;
          }
          // A settled turn with nothing held and no child is a decline the
          // second ask can fix; everything else waits out the probe.
          if (!held && !session.current_turn_id) return null;
          await sleep(2_000);
        }
        throw new Error(
          `the launch window never opened within ${LAUNCH_MS}ms. `
            + `fault_consumed=${JSON.stringify(readFrameHoldFault(frameHoldPath)?.consumed ?? null)}; `
            + `child_runs=${JSON.stringify((await api.listChildRuns(sessionId)).child_runs)}. `
            + 'If the declaration was never consumed, this deployment is not running with '
            + `ASTRABOX_E2E_FAULTS=1 or ${frameHoldPath} is not the path the backend reads. `
            + 'If it WAS consumed and no child started, the parent reached for another tool '
            + 'first and the barrier parked ahead of the launch.',
        );
      },
      what: 'the model answered without launching a run_in_background Agent — this spec\'s '
        + 'subject only exists once a detached child is running',
      budgetMs: LAUNCH_MS,
      probeMs: LAUNCH_MS,
    });

    // ── PRECONDITION: the child is running and the platform holds no record
    //    of it. Both halves, or the window below is not the one named. ───────
    const beforeLoss = await api.getSession(sessionId);
    launcherTurnId = String(beforeLoss.current_turn_id || '').trim();
    expect(
      launcherTurnId,
      'the launcher turn must still be the current turn — the barrier is inside it',
    ).not.toBe('');
    expect(
      openedManifests(sessionEvents(sessionId)),
      'nothing durable may know about the child yet; if it already does, the barrier '
        + 'parked after the terminal and this is not the window under test',
    ).toHaveLength(0);

    // ── THE LOSS: the host's handle on this session, and only that. ─────────
    const sandbox = await requireSandboxHandle(api, sandboxId);
    await api.adminEvictRuntime(sessionId);
    const afterEvict = await api.adminSessionDetail(sessionId);
    expect(
      afterEvict.has_local_runtime,
      'eviction must drop the host runtime handle — that is the loss under test',
    ).toBe(false);
    expect(
      String(afterEvict.sandbox_id || '').trim(),
      'eviction must not replace the box: the child is running inside this one',
    ).toBe(sandboxId);
    expect(
      sandboxRunning(sandbox),
      'the sandbox must still be running after the eviction, or the child died with the '
        + 'host and this is a different story',
    ).toBe(true);

    // Let the parked worker go, into a link the eviction already closed.
    // Whether it detaches or writes an error terminal is the runtime's choice;
    // both are this journey and both drop the in-memory manifest.
    releaseFrameHoldFault(frameHoldPath);

    // ── The user sends nothing else. The launcher turn has to settle. ───────
    const settled = await api.waitForSession(
      sessionId,
      (record) => !String(record.current_turn_id || '').trim()
        && !TERMINAL_SESSION_STATES.has(String(record.state || '')),
      SETTLE_MS,
    );
    settledLastTurnStatus = String(settled.last_turn_status || '');

    // ── Record the platform's tracking claim while the child is still at
    //    work. Recorded, not asserted: the anti-vacuity check comes first. ────
    tracking = await observeBackgroundTracking(api, sessionId, TRACK_MS);

    // ── ANTI-VACUITY, asserted before anything about tracking: the work
    //    really happened, in the live box, after the host lost the turn. ──────
    await expect
      .poll(() => sentinelReadable(api, platform, sessionId, donePath, 'done'), {
        timeout: MATERIALIZE_MS,
        intervals: [1_000, 2_000],
        message:
          `the background child must finish its work in the live box (${donePath}). `
          + 'If it never does, the child died with the host runtime — a different defect '
          + 'from the one this spec is about, and the assertions below would be reports '
          + 'about nothing.',
      })
      .toBe(true);

    // ── (1) The platform tracked the child it launched. ────────────────────
    const openedDiag = `journal_event_types=${JSON.stringify(tracking.lastEventTypes)}; `
      + `background_task_state=${JSON.stringify(
        tracking.lastSession ? tracking.lastSession.background_task_state ?? null : null,
      )}`;
    expect(
      tracking.openedEvents.map((event) => String(event.turn_id || '')),
      'exactly one turn.background_tasks_opened must name the launcher turn. The manifest '
        + 'is held in memory (workers/turn/state.py:52) and journaled only at '
        + 'workers/turn/bridge_terminal.py:605-606, so a worker lost inside the launch '
        + `window leaves the child untracked. ${openedDiag}`,
    ).toEqual([launcherTurnId]);
    expect(
      tracking.sawOpenWindow,
      'the session must report an OPEN background window at least once — it is the only '
        + 'source the console has for "background work is running" '
        + `(background_continuation.py:89-134 -> SessionPage.tsx:503). ${openedDiag}`,
    ).toBe(true);

    const openedEventSeq = Number(tracking.openedEvents[0].event_seq);
    expect(
      Number.isSafeInteger(openedEventSeq) && openedEventSeq > 0,
      'the open manifest must carry its journal sequence — materialization is claimed against it',
    ).toBe(true);

    // ── (2) The result reaches the user with nobody attached. ──────────────
    const closed = await api.waitForChildRuns(
      sessionId,
      (rows) => rows.some((row) => row.closed),
      MATERIALIZE_MS,
    );
    expect(
      closed.filter((row) => row.engine_kind === 'claude_code' && row.engine_status === 'completed'
        && row.closed),
      'the launched child must close with the engine\'s own completed status, carried '
        + 'verbatim — the platform re-encodes no vendor vocabulary',
    ).not.toHaveLength(0);

    const finalEvents = sessionEvents(sessionId);
    expect(
      materializationsForOpenedEvent(finalEvents, openedEventSeq),
      'the open manifest must be claimed exactly once — materialized, not replayed twice',
    ).toHaveLength(1);

    // ── (3) The user comes back to the conversation and reads the result.
    //    A cold open: the page rebuilds its child-run registry from the
    //    Session resource, which is the read path anyone checking on a
    //    background result actually takes. ──────────────────────────────────
    await openSessionView(page, sessionId);
    await expect(
      page.getByTestId('user-message').filter({ hasText: marker }),
      'the launcher message the user sent must still be in the transcript',
    ).toHaveCount(1, { timeout: 30_000 });
    await expect
      .poll(
        () => page.getByTestId('assistant-message').filter({ hasText: `${marker}_DONE` }).count(),
        {
          timeout: MATERIALIZE_MS,
          message:
            `the child's completion marker ${marker}_DONE must surface in the rendered `
            + 'transcript. Materialization is the only route that can put it there with '
            + 'nobody attached; a conversation that lost its manifest never reports the '
            + 'work it is still doing.',
        },
      )
      .toBeGreaterThan(0);

    await page.getByRole('tab', { name: /^Agents/ }).click();
    const agentsPanel = page.getByTestId('subagent-agents-panel');
    await expect(agentsPanel).toBeVisible({ timeout: 15_000 });
    await expect
      .poll(() => agentsPanel.getByTestId('subagent-agent-row').count(), {
        timeout: MATERIALIZE_MS,
        message: 'the launched child must render a row in the Agents panel',
      })
      .toBeGreaterThanOrEqual(1);
    // Exact match on the badge, not `hasText` on the row: a row's subtitle can
    // itself contain the word completed.
    await expect(
      agentsPanel.getByText(/^completed$/).first(),
      'the Agents panel must close the child out at the engine\'s exact status',
    ).toBeVisible({ timeout: MATERIALIZE_MS });

    // And the header settles. A conversation that recovered its child but is
    // left pulsing tells the user work is still running.
    await expect(
      page.getByTestId('run-view').getByTestId('status-pill').first(),
      'a conversation that lost its launcher must not be left pulsing',
    ).toHaveAttribute('data-pulse', 'false', { timeout: MATERIALIZE_MS });

    console.log('BACKGROUND_LAUNCH_LOSS_E2E_EVIDENCE', JSON.stringify({
      session_id: sessionId,
      sandbox_id: sandboxId,
      launcher_turn_id: launcherTurnId,
      // Diagnostic only, never the verdict: the transcript-recovery lane
      // settles provisionally and projects from the mirror, so a pinned value
      // here would assert an implementation route.
      last_turn_status: settledLastTurnStatus,
      held_frame_type: HELD_FRAME_TYPE,
      child_sentinels: { started: startedPath, done: donePath },
      journal_event_types: eventTypes(finalEvents),
      child_runs: closed.map((row: ChildRunRecord) => ({
        child_run_id: row.child_run_id,
        engine_kind: row.engine_kind,
        engine_event: row.engine_event,
        engine_status: row.engine_status,
        closed: row.closed,
      })),
      opened_event_seq: openedEventSeq,
    }));
  } finally {
    // The declaration goes first and unconditionally. A hold nobody releases
    // raises on the backend after its own 120s timeout and fails the turn for a
    // reason unrelated to this spec, and a file left behind would park the next
    // turn in this deployment. `trackSessions()` still decides whether the
    // session itself survives for diagnosis.
    releaseFrameHoldFault(frameHoldPath);
    clearFrameHoldFault(frameHoldPath);
    test.info().annotations.push({
      type: 'e2e_launcher_last_turn_status',
      description: settledLastTurnStatus || '<unsettled>',
    });
  }
});
