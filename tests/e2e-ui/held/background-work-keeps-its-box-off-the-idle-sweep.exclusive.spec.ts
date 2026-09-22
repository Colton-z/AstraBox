/**
 * E2E: work a user left running in the background survives the idle window they
 * spent away from the conversation.
 *
 * THE JOURNEY. A user launches a background Agent that will take longer than
 * their conversation's idle window, then walks away — back to the session list,
 * nothing typed, nothing clicked, nothing sent. The platform's idle keeper runs
 * while they are gone. When they come back the child must still be executing in
 * a live box and must still be able to finish.
 *
 * THE WOUND THIS GATES. Parking is decided by two functions and neither has
 * heard of background work. `_idle_window_if_parking`
 * (expiration_watcher.py:482-547) asks the Environment for `idle_action` and the
 * Agent for `idle_hibernate_seconds` and returns a window; `_is_idle_past`
 * (:549-577) reads the session snapshot's `conversation_state` and `updated_at`
 * and nothing else. A conversation whose parent turn has ended reads IDLE, and
 * nothing refreshes that clock on a working child's behalf — the resident
 * relay's between-runs path writes journal rows only (`_observe_idle`,
 * resident_relay.py:408-436), and the snapshot's `updated_at` moves only on a
 * channel or force update (session_snapshot_repository.py:147, :268), which are
 * turn-lifecycle writes. So the keeper sees a quiet conversation past its
 * window, renews to retention, marks it parked, pauses the Pod and evicts the
 * runtime (`_park_sandbox`, :358-425) — out from under a child that was working.
 *
 * WHY IT ASSERTS A PROPERTY AND NOT A CALL. Two shapes of fix are honest here:
 * teaching the park predicate to read the background manifest, or having
 * background work keep the snapshot clock warm. Both leave this spec green,
 * because what it asserts is the outcome — a box with open background work is
 * not parked, and the child is still running in it afterwards — rather than
 * which function learned something.
 *
 * WHAT MAKES IT A GATE AND NOT A HOPE. Four things can make "the box was not
 * parked" mean nothing, and each has its own arm:
 *
 *   - the keeper never ran, or pause is not honoured on this cluster → the
 *     CONTROL conversation, on the same Agent with the same window and the same
 *     Environment and no background work, MUST be parked. If it is not, the run
 *     is refused rather than passed.
 *   - the two conversations shared a box → `_box_is_this_session_s_alone`
 *     (:427-467) would refuse BOTH parks and the control arm would be satisfied
 *     by the wrong cause. The Environment is written with conversation tenancy
 *     and the two sandbox ids are asserted distinct.
 *   - the subject was never a candidate → every check re-reads the subject's own
 *     row against `list_idle_reclaim_candidates`'s predicate
 *     (session_repository.py:536-549) and requires it to still match.
 *   - the tick was truncated before it reached the subject → the sweep takes at
 *     most `_IDLE_SWEEP_SCAN_LIMIT` (expiration_watcher.py:40) candidates, so a
 *     tick reporting FEWER than that returned every matching row and necessarily
 *     included the subject. At least one such tick is required.
 *
 * DRIVEN THROUGH THE PRODUCT'S OWN ROUTE. `POST /admin/sandbox-idle-sweep` runs
 * `_expiration_watcher.scan_once()` — the identical tick the timer runs
 * (sandboxes.py:469-489) — so the spec neither re-implements the sweep nor waits
 * out a watcher interval, and its verdict does not depend on the lane having
 * configured a short one. The idle window itself is not faked either: the Agent
 * carries its own `idle_hibernate_seconds` (agent_schema.py:183), which
 * `_idle_window_if_parking` prefers over the 1800s deployment default, and the
 * spec then really waits it out. Nothing backdates the snapshot clock, because
 * that clock standing still IS half of the finding.
 *
 * THE PREMISE IS OPERATOR CONFIGURATION, NOT THE LANE DEFAULT. Under
 * `idle_action: "terminate"` — which is what the Agent Environments this suite
 * is provisioned with carry —
 * `_idle_window_if_parking` returns None and the sweep has nothing to do, so
 * the journey has no configuration in which it can fail or pass. The spec
 * therefore writes its own Environment through the real admin route
 * (`PUT /api/v1/admin/environments/{name}`, admin.py), cloned from the Agent the
 * lane selected so the ENGINE stays whichever the matrix chose, with
 * `idle_action: "pause"` and `sandbox_tenancy: "conversation"` overridden — and
 * FAILS LOUDLY if the backend refuses pause (environment_schema.py refuses it on
 * a backend that cannot snapshot) rather than degrading into a terminate-mode
 * run that proves nothing.
 *
 * ENGINE. Any matrix profile. All four declare `contracts.background_subagent`
 * and a `tools.command`, and the launch prompt names neither a vendor tool nor a
 * vendor flag — it describes background launch the way
 * `instructions.controllable_child` does for each profile. Nothing downstream
 * asserts a vendor status word either: the child's terminal is read off the
 * platform's own `closed` flag, since `child_completion.engine_statuses` differs
 * per profile and belongs to the engine rather than to the platform. A model
 * that answers
 * without launching anything is re-asked through `insist` and then FAILS — never
 * skipped, because a skip here is indistinguishable from the defect.
 *
 * EXCLUSIVE, AND NOT DEFENSIVELY. `scan_once()` sweeps the whole deployment —
 * startup allocations, abandoned Agent boxes, ownerless boxes, prepared-slot
 * renewal, dead bindings, then idle bindings — over every session and every box.
 * Run beside parallel workers it would reap and park theirs. It belongs in the
 * suite contract's one-worker serial group with the other park/sweep specs.
 *
 * WHAT THIS SPEC DOES NOT PROVE, and must not be read as proving: the other edge
 * of the same wound on the DEFAULT `terminate` deployment. There the box does not
 * park, it DIES at `sandbox_lease_seconds` with its workspace, because
 * `maybe_renew_lease_on_activity` is only ever called from turn paths — a
 * detached child renews nothing. No browser lane can age a real control-plane
 * lease inside 180s, so that half belongs in a Python test beside
 * `tests/sandbox_idle_parking_test.py`. Fixing only the park predicate leaves the
 * terminate default broken.
 *
 * WHAT A FAILURE LEAVES BEHIND: `trackSessions` keeps both sessions and their
 * boxes and names them in the report tail — the subject's box PAUSED if the
 * keeper got to it (retained for `sandbox_parked_retention_seconds`, 7 days by
 * default), the control's PAUSED by design. The Agent and the Environment are
 * also left in place on a failure; the Environment is disabled rather than
 * deleted on a pass, because `admin.py` offers environments only GET and PUT.
 */
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { sessionDoc, sessionEvents, snapshotDoc } from '../fixtures/dbOracle';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { insist } from '../fixtures/insist';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { expectComposerEnabled, openSessionView } from '../fixtures/sessionPage';

// The console detects language as ['localStorage','navigator'], so an unpinned
// runner locale decides which spelling the header pill gets, and a spec that
// accepted two spellings would also accept a third nobody wrote it against.
// Pinned the way agent-per-session-sandbox.exclusive.spec.ts:33 does.
test.use({ locale: 'en-US' });

// The masked stand-in the server returns for a stored model key
// (agent_config_service.py:60). Cloning it under a NEW environment name would
// resolve to "no stored value" and silently drop the credential, so the spec
// refuses the clone instead of running an Agent that cannot reach a model.
const ENV_API_KEY_SENTINEL = '••••••••';

// Per-tick parking budget in the product (`_IDLE_SWEEP_SCAN_LIMIT`,
// expiration_watcher.py:40). Mirrored here for one purpose: a tick reporting
// FEWER candidates than this took every row the candidate query matched, so a
// subject that matched the query was necessarily among them. A tick at or above
// it was truncated and can prove nothing about this conversation.
const IDLE_SWEEP_SCAN_LIMIT = 10;

// The window this Agent asks to be allowed to sit quiet, really waited out
// rather than simulated. Tunable so a lane that is short of wall can shrink it;
// the assertions read the Agent's stored value, never this constant directly.
const IDLE_WINDOW_MS = parseTimeoutEnv('ASTRABOX_E2E_IDLE_WINDOW_MS', 20_000);
const IDLE_WINDOW_SECONDS = Math.ceil(IDLE_WINDOW_MS / 1_000);

const READY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_READY_TIMEOUT_MS', 90_000);
// First evidence that the model actually backgrounded a subagent. Tight on
// purpose: the rest of this wall belongs to the window, the sweep and the child.
const BACKGROUND_PROBE_TIMEOUT_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_BACKGROUND_IDLE_PROBE_TIMEOUT_MS',
  60_000,
);
// How long both conversations may take to go quiet past their window.
const QUIET_TIMEOUT_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_IDLE_QUIET_TIMEOUT_MS',
  IDLE_WINDOW_SECONDS * 1_000 + 45_000,
);
// The child closing after its release, and the page dropping the background pill.
const CHILD_SETTLED_TIMEOUT_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_BACKGROUND_IDLE_SETTLED_TIMEOUT_MS',
  60_000,
);
// The child's own bound on the release gate: it raises rather than returning, so
// a run that dies before releasing leaves a failed child run instead of a silent
// success that proves nothing about ordering. Long enough to outlive the whole
// wall, short enough that nothing survives the round.
const CHILD_DEADLINE_SECONDS = 150;
// How many sweeps this spec drives. Two at minimum, because the first may land
// while the deployment's own watcher is mid-tick and report a truncated count.
const SWEEP_ATTEMPTS = 3;

// Sessions are deleted only when the test passes; a failure keeps both scenes
// and names them in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();
let agentId = '';
let parkingEnvironment = '';
let parkingEnvironmentPayload: Record<string, unknown> = {};

// Registration order is teardown order: the sessions go first (trackSessions,
// registered above), then their Agent, then the Environment nothing references.
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});
onPassOnly(async ({ request }) => {
  if (!parkingEnvironment) return;
  await new PlatformApi(request).putEnvironment(parkingEnvironment, {
    ...parkingEnvironmentPayload,
    enabled: false,
  });
});

/** The sweep tick's own counters, or an empty tick that found nothing to do. */
function sweepCounters(payload: Record<string, unknown>): Record<string, number> {
  const summary = payload.summary;
  if (summary == null) return {};
  if (typeof summary !== 'object' || Array.isArray(summary)) {
    throw new Error(`sandbox-idle-sweep summary must be an object; got ${JSON.stringify(summary)}`);
  }
  return Object.fromEntries(
    Object.entries(summary as Record<string, unknown>).map(([key, value]) => [key, Number(value)]),
  );
}

function parkedMark(sessionId: string): string {
  return String(sessionDoc(sessionId)?.sandbox_parked_at || '').trim();
}

/** Milliseconds since the snapshot clock the keeper reads last moved, or null. */
function quietForMs(sessionId: string): { state: string; quietMs: number | null } {
  const snapshot = snapshotDoc(sessionId);
  if (!snapshot) return { state: '<no snapshot>', quietMs: null };
  const updatedAt = Date.parse(String(snapshot.updated_at || ''));
  return {
    state: String(snapshot.conversation_state || ''),
    quietMs: Number.isFinite(updatedAt) ? Date.now() - updatedAt : null,
  };
}

/**
 * Whether this conversation still has background work the platform calls OPEN.
 *
 * Read from the journal rather than over HTTP, because between the walk-away and
 * the return this spec must not touch the session through anything that could
 * ensure or attach a runtime — it would then be performing the presence it is
 * trying to observe (the discipline
 * reconcile-is-present-for-a-parked-session-without-recovering-it.exclusive.spec.ts:129-132
 * states). This mirrors `_get_background_task_state`
 * (background_continuation.py:89-131) exactly: an opened manifest whose
 * event_seq no materialized event names is still open.
 */
function openBackgroundManifests(sessionId: string): number[] {
  const events = sessionEvents(sessionId);
  const opened = events.filter((event) => event.event_type === 'turn.background_tasks_opened');
  const materialized = new Set(
    events
      .filter((event) => event.event_type === 'turn.background_tasks_materialized')
      .map((event) => {
        const payload = event.payload;
        if (!payload || typeof payload !== 'object') return 0;
        return Number((payload as Record<string, unknown>).source_opened_event_seq || 0);
      })
      .filter((seq) => seq > 0),
  );
  return opened
    .map((event) => Number(event.event_seq || 0))
    .filter((seq) => seq > 0 && !materialized.has(seq));
}

/**
 * Restate the candidate query's predicate against this session's own row.
 *
 * Without it, "the subject was not parked" is satisfied for free by a row the
 * sweep never looked at — a lapsed lease, a cleared sandbox pointer, a
 * `runtime_unavailable` mark. Mirrors `list_idle_reclaim_candidates`
 * (session_repository.py:536-549); the failure names the field that disqualified
 * the row rather than reporting a vague "not a candidate".
 */
function disqualifiedFromTheSweep(sessionId: string): string {
  const row = sessionDoc(sessionId);
  if (!row) return 'the session row is gone';
  if (row.deleted === true) return 'the row is marked deleted';
  if (row.runtime_unavailable === true) return 'the row is marked runtime_unavailable';
  if (!String(row.sandbox_id || '').trim()) return 'the row no longer names a sandbox';
  const state = String(row.state || '');
  if (state === 'TERMINATED' || state === 'DELETED') return `the row reached ${state}`;
  const expiresAt = Date.parse(String(row.expires_at || ''));
  if (!Number.isFinite(expiresAt)) return `the row has no readable expires_at (${String(row.expires_at)})`;
  if (expiresAt <= Date.now()) {
    return `the sandbox lease already lapsed at ${String(row.expires_at)} — the dead-binding sweep owns this row, not the idle sweep`;
  }
  return '';
}

/** The child's program: append a stamped line every second until released. */
function heartbeatProgram(heartbeatPath: string, releasePath: string, doneMarker: string): string {
  return [
    "python3 - <<'PY'",
    'from pathlib import Path',
    'import time',
    `beat = Path(${JSON.stringify(heartbeatPath)})`,
    `release = Path(${JSON.stringify(releasePath)})`,
    'start = time.monotonic()',
    `deadline = start + ${CHILD_DEADLINE_SECONDS}`,
    'while not release.exists():',
    '    with beat.open("a") as handle:',
    '        handle.write("%.1f\\n" % (time.monotonic() - start))',
    '    if time.monotonic() >= deadline:',
    '        raise TimeoutError("the test never released the background subagent")',
    '    time.sleep(1)',
    `print(${JSON.stringify(doneMarker)})`,
    'PY',
  ].join('\n');
}

async function showAgentsTab(page: Page): Promise<void> {
  await page.getByRole('tab', { name: /^Agents/ }).click();
  await expect(page.getByTestId('subagent-agents-panel')).toBeVisible({ timeout: 15_000 });
}

function headerStatusPill(page: Page) {
  return page.getByTestId('run-view').locator('header').getByTestId('status-pill');
}

test('a background Agent still working keeps its box off the idle sweep while nobody is in the conversation', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const runId = new Date().toISOString().replace(/[:.]/g, '-');
  const parentMarker = `PARENT_LAUNCHED_${runId}`;
  const doneMarker = `BG_IDLE_DONE_${runId}`;

  // ── A deployment that parks idle boxes, one conversation to a box ─────────
  // Cloned from the Agent the lane selected, so the engine stays whichever the
  // matrix chose; only the two properties this journey rests on are overridden.
  const laneAgent = await api.defaultAgent();
  const sourceName = String(laneAgent.environment_name || '').trim();
  expect(
    sourceName,
    `the lane's Agent ${JSON.stringify(laneAgent.name)} must name the Environment this journey clones`,
  ).not.toEqual('');
  const environments = await platform.listEnvironments();
  const source = environments.find((item) => String(item.name || '') === sourceName);
  expect(
    source,
    `Environment ${JSON.stringify(sourceName)} is not on this deployment; have: `
      + environments.map((item) => String(item.name || '')).join(', '),
  ).toBeTruthy();
  const sourceEnvironment = source as Record<string, unknown>;
  const access = sourceEnvironment.provider_access;
  if (access && typeof access === 'object') {
    expect(
      String((access as Record<string, unknown>).api_key || ''),
      'the source Environment stores a model key that a clone cannot carry — the server '
        + 'returns it masked and a clone under a new name would store nothing. Point this '
        + 'lane at an Environment using deployment model access.',
    ).not.toEqual(ENV_API_KEY_SENTINEL);
  }

  // Project through the product's own schema rather than a list kept here: a
  // second copy of the editable surface would silently stop carrying whatever
  // field is added to the first.
  const schema = await platform.environmentSchema();
  const editableKeys = (schema.fields || [])
    .map((field) => String(field.key || ''))
    .filter((key) => key && key !== 'name');
  expect(editableKeys.length, 'the environment schema must declare its editable fields').toBeGreaterThan(0);

  // Environments on this deployment are named as lowercase dashed resources and
  // the name reaches a URL path; the `__e2e_` spelling belongs to throwaway
  // Agents, not to this collection.
  parkingEnvironment = `astrabox-e2e-tmp-idle-bg-${Date.now()}`;
  parkingEnvironmentPayload = {
    ...Object.fromEntries(
      editableKeys
        .filter((key) => key in sourceEnvironment)
        .map((key) => [key, sourceEnvironment[key]]),
    ),
    display_name: 'Background work over an idle window (E2E)',
    description: `Created by ${test.info().titlePath.join(' › ')}.`,
    enabled: true,
    // The keeper only has business with a conversation whose Environment says
    // pause; under terminate `_idle_window_if_parking` returns None.
    idle_action: 'pause',
    // A box two conversations point at is refused by `_box_is_this_session_s_alone`,
    // which would make BOTH arms of this spec vacuous at once.
    sandbox_tenancy: 'conversation',
  };
  let stored: Record<string, unknown>;
  try {
    stored = await platform.putEnvironment(parkingEnvironment, parkingEnvironmentPayload);
  } catch (error) {
    parkingEnvironment = '';
    throw new Error(
      'this deployment refused an Environment that parks idle boxes, so it has no '
        + 'configuration in which this journey can fail or pass. Under idle_action '
        + '"terminate" the keeper leaves every box alone and the box instead dies at its '
        + `lease, which no 180s spec can reach.\n${error instanceof Error ? error.message : String(error)}`,
    );
  }
  expect(
    String(stored.idle_action || ''),
    'the stored Environment must be the one the keeper will read',
  ).toEqual('pause');
  expect(
    String(stored.sandbox_tenancy || ''),
    'each conversation must own its box, or the keeper refuses to park either of them '
      + 'and both arms of this spec pass for the wrong reason',
  ).toEqual('conversation');

  // ── An Agent allowed to sit quiet for exactly the window ─────────────────
  const model = String(laneAgent.model || '').trim();
  expect(model, 'the lane-selected Agent must name a model route').not.toEqual('');
  expect(
    await api.listEnvironmentModels(parkingEnvironment),
    `the parking Environment must expose the lane's model ${JSON.stringify(model)}`,
  ).toContain(model);

  const agent = await api.createAgent({
    name: `__e2e_idle_bg_${runId}`,
    model,
    environment_name: parkingEnvironment,
    // No prepared slot: this Agent must not spend its setup preparing a warm box
    // it never claims, and a pooled box is not this conversation's alone.
    prewarm_enabled: false,
    idle_hibernate_seconds: IDLE_WINDOW_SECONDS,
  });
  agentId = String(agent.agent_id || '');
  expect(agentId, 'the Agent must be created').not.toEqual('');
  // `createAgent` projects its payload through the authoring schema and drops
  // anything the schema does not declare. A dropped window is invisible: the
  // sweep would fall back to the 1800s deployment default and judge this Agent
  // by a clock the spec never set, and nothing would park inside this wall.
  expect(
    Number((await api.getAgent(agentId)).idle_hibernate_seconds),
    'the Agent must carry the idle window this journey waits out',
  ).toBe(IDLE_WINDOW_SECONDS);

  // ── The conversation the user leaves work running in ─────────────────────
  const subjectCreated = await api.startConversation(agentId);
  const subject = String(subjectCreated.session_id || '').trim();
  expect(subject, 'starting a conversation must open a session').not.toEqual('');
  sessions.push(subject);
  const subjectReady = await api.waitForSessionReady(subject, READY_TIMEOUT_MS);
  const subjectSandbox = String(subjectReady.sandbox_id || '').trim();
  expect(subjectSandbox, 'a READY conversation must name its box').not.toEqual('');
  // Explicit, so the child's one command call runs unattended instead of resting
  // on whatever default the conversation was created with.
  await api.setPermissionMode(subject, 'bypassPermissions');

  // The workspace root comes from the product, never from a literal: the
  // short-cwd profile can move it, and the child's shell and the platform
  // terminal deliberately do not share /tmp.
  const rootListing = await platform.listFiles(subject);
  const root = String(rootListing.root_path || '').trim();
  expect(root, 'the files listing must name the workspace root the child writes into').not.toEqual('');
  const heartbeatName = `.astrabox-bg-heartbeat-${runId}.log`;
  const releaseName = `.astrabox-bg-release-${runId}`;
  const heartbeatPath = `${root}/${heartbeatName}`;
  const releasePath = `${root}/${releaseName}`;

  // ── The user launches the background work, from the real composer ────────
  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* localStorage unavailable — the en-US navigator locale still applies */
    }
  });
  await openSessionView(page, subject);
  await expectComposerEnabled(page);

  // Engine-agnostic by construction: every matrix profile declares a
  // `tools.command` and `contracts.background_subagent`, and the launch is
  // described the way each profile's `instructions.controllable_child` does
  // rather than in one vendor's flag name.
  const prompt = [
    `E2E background idle sweep ${runId}. This is a product regression test; follow the tool instructions literally.`,
    'Launch exactly one subagent that runs IN THE BACKGROUND — whatever your tools call that:',
    'running it in the background, launching it asynchronously, or not waiting for it.',
    'Its whole task is: use your shell command tool exactly once to run the command between the',
    `markers below, wait for that command, then report briefly with ${doneMarker}.`,
    '--- BEGIN COMMAND ---',
    heartbeatProgram(heartbeatPath, releasePath, doneMarker),
    '--- END COMMAND ---',
    'Do not run that command yourself, do not launch a second subagent, and do not wait for this one.',
    `Immediately after launching it, reply with one short sentence containing ${parentMarker} and end your turn.`,
  ].join('\n');
  const composer = page.getByTestId('composer-prompt');
  // `.fill()` sets the value without key events, so the multi-line launch prompt
  // is not submitted early by an Enter newline.
  await composer.fill(prompt);
  const submit = page.getByTestId('composer-submit');
  await expect(submit).toBeEnabled({ timeout: 15_000 });
  await submit.click();

  // ── The control conversation, started while the launch turn runs ─────────
  // Same Agent, same window, same Environment, no background work. Created here
  // rather than after the walk-away so its own quiet window elapses alongside
  // the subject's instead of after it.
  const controlCreated = await api.startConversation(agentId);
  const control = String(controlCreated.session_id || '').trim();
  expect(control, 'the control conversation must open a session').not.toEqual('');
  sessions.push(control);

  // ── Did the model actually background a subagent? ────────────────────────
  // Ask again rather than skip: a runtime test.skip() SIGTERMs the worker and
  // the lane counts the round exactly as a failure, so the model's choice would
  // decide the round instead of the platform's behaviour — and a skip here is
  // indistinguishable from the defect this spec exists to catch.
  await insist<true>({
    ask: async (attempt) => {
      if (attempt > 1) await api.postTurnInput(subject, prompt);
    },
    probe: async () => {
      const deadline = Date.now() + BACKGROUND_PROBE_TIMEOUT_MS;
      while (Date.now() < deadline) {
        const session = await api.getSession(subject);
        if (session.background_task_state) return true;
        const state = String(session.state || '');
        if (state === 'TERMINATED' || state === 'DELETED' || state === 'RECOVERY_REQUIRED') {
          throw new Error(`session ${subject} reached terminal state ${state} during the probe`);
        }
        await page.waitForTimeout(1_500);
      }
      return null;
    },
    what:
      `no open background task on ${subject} within ${BACKGROUND_PROBE_TIMEOUT_MS}ms: the model `
      + 'answered inline instead of launching a background subagent, and this spec\'s subject '
      + 'only exists while one is running',
    budgetMs: BACKGROUND_PROBE_TIMEOUT_MS * 2,
    probeMs: BACKGROUND_PROBE_TIMEOUT_MS,
  });

  const controlReady = await api.waitForSessionReady(control, READY_TIMEOUT_MS);
  const controlSandbox = String(controlReady.sandbox_id || '').trim();
  expect(controlSandbox, 'the control conversation must name its box').not.toEqual('');
  expect(
    controlSandbox,
    'the two conversations must hold different boxes: a shared box makes '
      + '`_box_is_this_session_s_alone` refuse BOTH parks, and the control arm would then be '
      + 'satisfied by the wrong cause',
  ).not.toEqual(subjectSandbox);

  test.info().annotations.push({
    type: 'idle_background_scene',
    description: JSON.stringify({
      subject, subjectSandbox, control, controlSandbox, agentId,
      environment: parkingEnvironment, window_seconds: IDLE_WINDOW_SECONDS,
    }),
  });

  // ── This is the state the user leaves behind ─────────────────────────────
  await showAgentsTab(page);
  await expect(
    page.getByTestId('subagent-agents-panel').getByTestId('subagent-agent-row').first(),
    'the Agents panel must show the child the user is leaving running',
  ).toBeVisible({ timeout: 45_000 });
  const pill = headerStatusPill(page);
  await expect(
    pill,
    'the header must say the work is running in the background before the user walks away',
  ).toHaveText('Running in background', { timeout: 45_000 });
  await expect(pill).toHaveAttribute('data-state', 'BACKGROUND_RUNNING');

  // ── The user walks away ──────────────────────────────────────────────────
  // From here until the return, nothing in this spec touches the subject over
  // HTTP: every reading below comes from the document store, because a read
  // through an endpoint that attaches would be the test performing the presence
  // it is trying to observe.
  await page.goto(appPath('/sessions'), { waitUntil: 'domcontentloaded' });

  // ── Both conversations go quiet past the window they were given ──────────
  // Really waited, not backdated: the snapshot clock standing still while a
  // child works is half of the finding, and a spec that wrote that clock itself
  // could not tell the product's silence from its own.
  const quietDeadline = Date.now() + QUIET_TIMEOUT_MS;
  for (;;) {
    const subjectQuiet = quietForMs(subject);
    const controlQuiet = quietForMs(control);
    const bothQuiet =
      subjectQuiet.state === 'IDLE' && (subjectQuiet.quietMs ?? -1) >= IDLE_WINDOW_MS
      && controlQuiet.state === 'IDLE' && (controlQuiet.quietMs ?? -1) >= IDLE_WINDOW_MS;
    if (bothQuiet) break;
    expect(
      parkedMark(subject),
      'the idle sweep paused the box of a conversation whose background subagent was still '
        + `running, before either conversation had even finished its window (session=${subject} `
        + `sandbox=${subjectSandbox} quiet=${subjectQuiet.quietMs}ms state=${subjectQuiet.state})`,
    ).toEqual('');
    if (Date.now() >= quietDeadline) {
      throw new Error(
        `both conversations must be IDLE past their ${IDLE_WINDOW_SECONDS}s window before the `
          + `keeper can be asked about them, and they did not within ${QUIET_TIMEOUT_MS}ms. `
          + `subject=${JSON.stringify(subjectQuiet)} control=${JSON.stringify(controlQuiet)}. `
          + 'A subject that never settles means the launch turn is still running; a control '
          + 'that never settles means its conversation is doing something it was never asked to.',
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }

  // ── The platform's keeper runs over both conversations ───────────────────
  const ticks: Array<Record<string, number>> = [];
  let controlParkedAt = parkedMark(control);
  let sawUntruncatedTick = false;
  for (let attempt = 1; attempt <= SWEEP_ATTEMPTS; attempt += 1) {
    // Restated before every tick: a subject the sweep could not have looked at
    // proves nothing by surviving it.
    const disqualified = disqualifiedFromTheSweep(subject);
    expect(
      disqualified,
      `the subject stopped qualifying for the idle sweep, so its survival proves nothing: `
        + `${disqualified} (session=${subject} sandbox=${subjectSandbox})`,
    ).toEqual('');
    const stillOpen = openBackgroundManifests(subject);
    expect(
      stillOpen.length,
      'the background subagent closed before the keeper ran, so there was no work for the '
        + `keeper to take a box away from (session=${subject}). The child is gated on a release `
        + 'file this spec has not written yet, so a closed manifest here means it failed.',
    ).toBeGreaterThan(0);

    const tick = sweepCounters(await platform.idleSweep());
    ticks.push(tick);
    const candidates = Number(tick.idle_candidates || 0);
    if (candidates >= 1 && candidates < IDLE_SWEEP_SCAN_LIMIT) sawUntruncatedTick = true;

    const subjectParkedAt = parkedMark(subject);
    const subjectQuiet = quietForMs(subject);
    expect(
      subjectParkedAt,
      'THE FINDING: the idle sweep paused the box of a conversation whose background subagent '
        + `was still running. session=${subject} sandbox=${subjectSandbox} `
        + `parked_at=${subjectParkedAt} idle_age=${subjectQuiet.quietMs}ms `
        + `window=${IDLE_WINDOW_SECONDS}s open_manifests=${JSON.stringify(stillOpen)} `
        + `tick=${JSON.stringify(tick)}. `
        + '`_idle_window_if_parking` (expiration_watcher.py:482) and `_is_idle_past` (:549) '
        + 'never ask whether work is running, and nothing refreshes the snapshot clock on a '
        + 'detached child\'s behalf — so a quiet conversation loses the compute its own work '
        + 'is using.',
    ).toEqual('');

    controlParkedAt = parkedMark(control);
    if (controlParkedAt && sawUntruncatedTick) break;
    await new Promise((resolve) => setTimeout(resolve, 3_000));
  }

  await test.info().attach('idle-sweep-ticks', {
    body: JSON.stringify({
      ticks,
      subject: { session: subject, sandbox: subjectSandbox, parked_at: parkedMark(subject), quiet: quietForMs(subject) },
      control: { session: control, sandbox: controlSandbox, parked_at: controlParkedAt, quiet: quietForMs(control) },
    }, null, 2),
    contentType: 'application/json',
  });

  // ── The run's own evidence that the keeper was armed and reached here ────
  expect(
    controlParkedAt,
    'the control conversation was never parked, so this run cannot claim the keeper would have '
      + `parked anything: same Agent, same ${IDLE_WINDOW_SECONDS}s window, same Environment, no `
      + `background work. Either the sweep is not parking on this deployment, or pause did not `
      + `commit on this cluster (docs/providers/opensandbox.md names what pausing needs), or the `
      + `two conversations shared a box. control=${control} sandbox=${controlSandbox} `
      + `ticks=${JSON.stringify(ticks)}`,
  ).not.toEqual('');
  expect(
    sawUntruncatedTick,
    `every tick this spec drove reported ${IDLE_SWEEP_SCAN_LIMIT} or more idle candidates (or `
      + 'none at all), which means the scan was truncated at `_IDLE_SWEEP_SCAN_LIMIT` and cannot '
      + 'be said to have reached this conversation. This deployment is holding too many live '
      + `conversations for the gate to mean anything — ticks=${JSON.stringify(ticks)}`,
  ).toBe(true);

  // ── The user comes back ──────────────────────────────────────────────────
  await openSessionView(page, subject);
  await showAgentsTab(page);
  await expect(
    headerStatusPill(page),
    'coming back must find the background work still running',
  ).toHaveText('Running in background', { timeout: 45_000 });

  // The work is still EXECUTING, not merely unparked in a database row. Two
  // reads of the child's own heartbeat, both taken after the sweep: the file has
  // to have grown between them. Comparing line counts rather than timestamps
  // keeps this on the box's clock alone, with no skew against the runner's.
  const firstBeat = (await api.downloadFileText(subject, heartbeatPath)).trim().split('\n').filter(Boolean);
  expect(
    firstBeat.length,
    `the background subagent wrote no heartbeat at all into ${heartbeatPath}; it never started `
      + 'its command, so nothing in this run was ever at risk from the sweep',
  ).toBeGreaterThan(0);
  await expect
    .poll(
      async () => (await api.downloadFileText(subject, heartbeatPath)).trim().split('\n').filter(Boolean).length,
      {
        timeout: 20_000,
        intervals: [1_500, 2_000],
        message:
          'the background subagent stopped executing: its heartbeat did not advance after the '
          + `idle sweep ran (last=${firstBeat.at(-1)}s into the child's own run, `
          + `session=${subject} sandbox=${subjectSandbox}). An unparked database row is not the `
          + 'same as work that survived.',
      },
    )
    .toBeGreaterThan(firstBeat.length);

  // ── And it can still finish ──────────────────────────────────────────────
  await api.uploadFileText(subject, root, releaseName, 'release');
  const closed = await api.waitForChildRuns(
    subject,
    (rows) => rows.length >= 1 && rows.every((row) => row.closed),
    CHILD_SETTLED_TIMEOUT_MS,
  );
  // At least one, not exactly one. The prompt asks for a single subagent, but a
  // model that launched two would still have left real background work running
  // across the sweep — failing here would reject the run for the model's choice
  // after the finding had already been proved. The count is annotated instead.
  expect(
    closed.length,
    'the released background subagent must close its child run',
  ).toBeGreaterThanOrEqual(1);
  test.info().annotations.push({
    type: 'closed_child_runs',
    description: JSON.stringify(
      closed.map((row) => ({
        child_run_id: row.child_run_id,
        engine_kind: row.engine_kind,
        engine_status: row.engine_status,
        engine_reason: row.engine_reason,
      })),
    ),
  });
  await expect
    .poll(
      async () => {
        const detail = await api.getSession(subject);
        return String(detail.state || '') === 'READY' && !detail.background_task_state;
      },
      {
        timeout: CHILD_SETTLED_TIMEOUT_MS,
        intervals: [1_000, 2_000],
        message: 'the completed background task must clear and the conversation project READY',
      },
    )
    .toBe(true);
  await expect(
    headerStatusPill(page),
    'the header must stop saying the work is running once the child has closed',
  ).not.toHaveText('Running in background', { timeout: 45_000 });
});
