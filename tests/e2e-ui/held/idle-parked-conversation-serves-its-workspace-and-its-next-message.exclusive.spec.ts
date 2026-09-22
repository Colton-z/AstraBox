/**
 * E2E: a conversation whose box the idle keeper parked comes back whole — its
 * own files, its own box, its own next turn — and the user is never told a box
 * was parked.
 *
 * THE JOURNEY. Somebody talks to an Agent, leaves a file in its workspace, and
 * closes the tab. The conversation goes quiet past its idle window, so the
 * keeper commits the box's filesystem, frees its compute and marks the binding
 * parked. Later the person opens the conversation again in a fresh tab, looks at
 * the Files panel, and sends another message. This is the whole product promise
 * that separates `idle_action: "pause"` from `terminate`: under terminate the
 * box dies with the workspace and the next turn cold-creates, which is a
 * different (and honest) product. Under pause the files are still there, the box
 * is the same box, and nothing about the return leg asks the user to know that.
 *
 * WHAT IS EXPECTED TO BE RED, AND WHY — read, not run. The Files panel is the
 * half that has no wake in it. `_wake_parked_sandbox` (runtime_ensure.py:485)
 * has exactly ONE caller: the turn-prepare path (:448). The product's other two
 * readers of `sandbox_parked_at` (sandbox_lifecycle.py:52 and :568) decide
 * whether a teardown was planned; neither reaches for a box.
 * `SessionFileService._resolve_session_context`
 * (session_file_service.py:521-551) resolves the box by id and goes straight to
 * `_get_or_connect_sandbox` against compute the keeper freed. Meanwhile the
 * panel is fully ENABLED over that row: parking writes only `sandbox_parked_at`
 * and `expires_at` with `touch_updated_at=False` (`_park_sandbox`,
 * expiration_watcher.py:397-405), so `state` stays READY, `sandbox_id` stays
 * set and `runtime_unavailable` stays false — which is exactly what
 * `isSandboxLiveForSession` (frontend/src/utils/format.ts:168) and
 * `filesPanelEnabled` (useSessionRightPanelState.ts:61) ask for. So the panel is
 * not disabled with an explanation; it is enabled and wrong. What this spec
 * cannot state from reading is the SHAPE of that wrongness — a spinner to the
 * control deadline, a fast refusal, or a listing that unexpectedly answers — so
 * the assertion polls one verdict function that reports whichever of those
 * happened, with the panel's own error text in the failure.
 *
 * NOT the symptom the finding was filed under. That wording said the panel would
 * read "runtime unavailable"; that is the opposite direction, and a spec written
 * to it would assert a branch this scenario cannot reach.
 *
 * THE RETURN MUST ALSO READ CALM, and that half is expected green: a parked
 * conversation still projects READY, so the header must say Ready and never
 * "Runtime disconnected" / "Recovery needed", and no "Recover session" control
 * may appear. If a fix makes the Files panel honest by making the SESSION look
 * broken, that is not a fix, and these assertions are what says so.
 *
 * THE ONE ASSERTION THAT CAN TELL PARKING FROM SILENT WORKSPACE LOSS is the
 * sandbox id after the second turn. A resume that did not take falls through to
 * the ordinary gone-sandbox re-borrow (runtime_ensure.py:524-529 logs "this turn
 * continues on a FRESH box and that workspace is not in it", and :467-483 is the
 * re-borrow itself), which still produces a perfectly good-looking reply. Byte
 * equality on the marker file is the second half of the same question: a
 * rebuilt box has neither.
 *
 * ENGINE: none required. The spec takes whichever Agent the matrix selected
 * (`ASTRABOX_E2E_AGENT_NAME`), clones that Agent's Environment and overrides one
 * field — `idle_action: "pause"` — so the engine_kind, runtime_template_name and
 * tenancy stay the profile's own. The prompts ask for one short sentence and
 * nothing downstream matches the model's words, so there is no `insist` and no
 * model-conditional skip.
 *
 * DEPLOYMENT: two requirements, both asserted loudly rather than skipped. (1)
 * The backend must declare pause — `GET /admin/sandbox-idle-action` lists it in
 * `supported_actions`; a backend that cannot snapshot has the Environment write
 * refused outright by `_assert_idle_action_is_reachable`
 * (environment_schema.py:365). (2) The cluster must actually be able to commit,
 * and capability is not readiness — there is no probe for that short of doing
 * it, so the spec discovers it at the park step and fails there with the
 * sweeper's own summary attached.
 *
 * DRIVEN THROUGH THE PRODUCT'S OWN ROUTE, twice over. The keeper's period is
 * `expiration_watcher_interval_seconds`, 300s by default (settings.py:491-497),
 * so `POST /admin/sandbox-idle-sweep` runs the same `scan_once()` the timer
 * runs (sandboxes.py:468-492). The idle window is not faked either: the
 * Agent carries its own `idle_hibernate_seconds` (agent_schema.py:183), which
 * `_idle_window_if_parking` prefers over the deployment's 1800s default, and the
 * spec waits it out on the snapshot clock the keeper itself reads. Nothing here
 * backdates a document.
 *
 * EXCLUSIVE, and not defensively: `scan_once()` sweeps the whole deployment and
 * would park, reap and converge other workers' scenes. It belongs with the other
 * park/sweep specs in the suite contract's one-worker serial group.
 *
 * THE BUDGET IS THE REAL THREAT, not the assertion — a cold box, two model
 * turns, a real snapshot commit and a wake in one 180s wall. If a first run runs
 * out of wall, cut in this order and do not raise the budget (the runner
 * hard-asserts `max_test_seconds === 180`): (1) drop the pre-park turn and plant
 * the marker by upload alone — parking only needs the conversation IDLE, which
 * `_initialize_create_snapshots` (session_kernel/workers/lifecycle/create.py:160)
 * already writes at creation, so the conversation need never have run a turn;
 * (2) split into two specs at the park boundary.
 *
 * WHAT IT LEAVES BEHIND. A failing run keeps the session and its parked box
 * (`trackSessions`), which is retained for `sandbox_parked_retention_seconds`.
 * The Agent is deleted on a pass. The Environment row is NOT: `admin.py` offers
 * environments only GET and PUT, so the name is stable and a rerun upserts the
 * same row rather than accumulating one per run. Each park also commits a rootfs
 * snapshot that nothing in AstraBox or OpenSandbox collects
 * (docs/maintainers/deploy-aws-eks.md) — on the single-host testbed that is
 * disk, one image per run.
 */
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { sessionDoc, snapshotDoc } from '../fixtures/dbOracle';
import { parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import {
  closePageAfterAssertions,
  expectComposerEnabled,
  openSessionView,
  sendPrompt,
} from '../fixtures/sessionPage';

// This spec asserts the console's localized calm: the header must read "Ready"
// and never "Runtime disconnected" / "Recovery needed", and no "Recover session"
// button may exist. i18next detects ['localStorage','navigator'], so an unpinned
// runner locale decides which spelling those are — and a UI rendering another
// language would false-green every negative assertion, because it never emits
// these strings in any state. Pinned in both places a user's choice lands, the
// way reclaimed-sandbox-reads-ready.exclusive.spec.ts:41 does in the other
// direction.
test.use({ locale: 'en-US' });

// The masked stand-in the server returns for a stored secret
// (agent_config_service.py:60). Cloning it under a NEW Environment name resolves
// to "no stored value", so the clone would silently drop the credential and the
// failure would surface three steps later as an unexplained model error.
const REDACTION_SENTINEL = '••••••••';

// The product's own word for a committed box (`_PAUSED_STATES`,
// providers/open_sandbox/sandbox.py:130). Not a spelling invented here.
const PAUSED_STATES = new Set(['PAUSED', 'SUCCEED']);

// How long this Agent may sit quiet before the keeper may take its compute.
// Really waited out on the snapshot clock, not simulated; short because the wall
// is 180s, and read back off the stored Agent rather than trusted from here.
const IDLE_WINDOW_MS = parseTimeoutEnv('ASTRABOX_E2E_IDLE_PARK_WINDOW_MS', 5_000);
const IDLE_WINDOW_SECONDS = Math.max(1, Math.ceil(IDLE_WINDOW_MS / 1_000));

const READY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_READY_TIMEOUT_MS', 90_000);
// One model turn rendering in the browser, either side of the park.
const REPLY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_IDLE_PARK_REPLY_TIMEOUT_MS', 60_000);
// The conversation settling to IDLE and staying there past its window. Its own
// name rather than the sweep spec's `ASTRABOX_E2E_IDLE_QUIET_TIMEOUT_MS`: that
// default is derived from a different window, so one override would be wrong for
// whichever spec did not set it.
const QUIET_TIMEOUT_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_IDLE_PARK_QUIET_TIMEOUT_MS',
  IDLE_WINDOW_MS + 45_000,
);
// The sweep tick, plus the snapshot commit it holds open. `pause_sandbox_by_id`
// waits on the control plane itself, so a tick that returns has already settled
// the box; this budget is for driving more than one tick when the scan is busy.
const PARK_SETTLE_MS = parseTimeoutEnv('ASTRABOX_E2E_PARK_SETTLE_MS', 90_000);
// The Files panel's own answer, whatever it turns out to be. Bounded by
// ASTRABOX_SANDBOX_CONTROL_DEADLINE_S on the failing side.
const FILE_PANEL_MS = parseTimeoutEnv('ASTRABOX_E2E_FILE_PANEL_TIMEOUT_MS', 60_000);

// Sessions are deleted only when the test passes; a failure keeps the scene and
// names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();
let agentId = '';

// Registration order is teardown order: the session goes first (trackSessions,
// above), then the Agent it ran on. The Environment is left in place by design —
// its name is stable and there is no DELETE route for environments.
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
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

/**
 * The platform's own statement that this binding is parked.
 *
 * Read from the document store rather than over HTTP, because between the
 * walk-away and the return this spec must not touch the session through anything
 * that could ensure or attach a runtime — it would then be undoing the condition
 * it came to observe (the discipline
 * reconcile-is-present-for-a-parked-session-without-recovering-it.exclusive.spec.ts
 * states). The keeper clears this mark again when the commit refuses
 * (`_clear_parked_mark`), so its presence after a tick returns is the product
 * saying parking completed, not that it was attempted.
 */
function parkedMark(sessionId: string): string {
  return String(sessionDoc(sessionId)?.sandbox_parked_at || '').trim();
}

/** State and quiet age off the snapshot clock `_is_idle_past` reads, and only that one. */
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
 * Restate the candidate query's predicate against this session's own row.
 *
 * Without it, "it was never parked" is a failure with no direction: a row the
 * sweep could not have looked at reads exactly like a keeper that refused. This
 * mirrors `list_idle_reclaim_candidates` (session_repository.py:536-549) and
 * names the field that disqualified the row.
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
    return `the sandbox lease lapsed at ${String(row.expires_at)} — the dead-binding sweep owns `
      + 'this row, not the idle sweep';
  }
  return '';
}

/** Whether any value carried by the cloned payload is a server-side redaction. */
function carriesRedactedSecret(value: unknown): boolean {
  if (typeof value === 'string') return value.includes(REDACTION_SENTINEL);
  if (Array.isArray(value)) return value.some(carriesRedactedSecret);
  if (value && typeof value === 'object') {
    return Object.values(value as Record<string, unknown>).some(carriesRedactedSecret);
  }
  return false;
}

test('a conversation parked for idleness opens on its own workspace and continues on the same box', async ({
  page,
  context,
  request,
}) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const runId = new Date().toISOString().replace(/[:.]/g, '-');

  // Both tabs, and every navigation of either, get the language the negative
  // assertions are written against. On the context rather than the page because
  // the return leg opens a second tab.
  await context.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {
      /* localStorage unavailable — the en-US navigator locale still applies */
    }
  });

  // ── PRECONDITION: this deployment can park at all ────────────────────────
  const capability = await platform.idleAction();
  const supported = (capability.supported_actions as unknown[] | undefined) || [];
  expect(
    supported.map((action) => String(action)),
    'this deployment\'s sandbox backend cannot snapshot, so `idle_action: "pause"` is refused '
      + 'at the Environment form and this journey has no configuration in which it can pass or '
      + `fail. The route's own reason: ${String(capability.detail ?? '<none given>')} `
      + `(backend=${String(capability.backend ?? '<unnamed>')})`,
  ).toContain('pause');

  // ── An Environment that parks, cloned from whichever engine the matrix chose ──
  const laneAgent = await api.defaultAgent();
  const sourceName = String(laneAgent.environment_name || '').trim();
  expect(
    sourceName,
    `the lane's Agent ${JSON.stringify(laneAgent.name)} must name the Environment this clones`,
  ).not.toEqual('');
  const environments = await platform.listEnvironments();
  const source = environments.find((item) => String(item.name || '') === sourceName);
  expect(
    source,
    `Environment ${JSON.stringify(sourceName)} is not on this deployment; have: `
      + environments.map((item) => String(item.name || '')).join(', '),
  ).toBeTruthy();
  const sourceEnvironment = source as Record<string, unknown>;

  // Project through the product's own schema rather than a field list kept here:
  // a second copy of the editable surface silently stops carrying whatever field
  // is added to the first, and the validator only checks keys the schema
  // declares — so projecting also drops computed reads like `engine_available`.
  const schema = await platform.environmentSchema();
  const editableKeys = (schema.fields || [])
    .map((field) => String(field.key || ''))
    .filter((key) => key && key !== 'name');
  expect(
    editableKeys.length,
    'the environment schema must declare its editable fields',
  ).toBeGreaterThan(0);

  // Stable, not run-scoped: environments have GET and PUT and no DELETE, so a
  // run-scoped name would leave one row per run on the deployment forever.
  const parkEnvironment = `__e2e-idle-park-${sourceName}`;
  const parkPayload = {
    ...Object.fromEntries(
      editableKeys
        .filter((key) => key in sourceEnvironment)
        .map((key) => [key, sourceEnvironment[key]]),
    ),
    // The only changed field. Everything else — engine_kind,
    // runtime_template_name, tenancy, networking — stays the profile's own, which
    // is what lets this spec run under every engine in the matrix.
    idle_action: 'pause',
  };
  expect(
    carriesRedactedSecret(parkPayload),
    'the source Environment stores a secret the server returns masked, and a clone under a new '
      + 'name stores nothing for it. Point this lane at an Environment that uses deployment '
      + 'model access, or the park Environment will fail its first model call for a reason that '
      + 'has nothing to do with parking.',
  ).toBe(false);

  let storedEnvironment: Record<string, unknown>;
  try {
    storedEnvironment = await platform.putEnvironment(parkEnvironment, parkPayload);
  } catch (error) {
    throw new Error(
      'this deployment refused an Environment that parks idle boxes, so the journey has no '
        + 'configuration in which it can pass or fail. Under idle_action "terminate" the keeper '
        + 'leaves every box alone and the box dies at its lease with the workspace instead.\n'
        + `${error instanceof Error ? error.message : String(error)}`,
    );
  }
  expect(
    String(storedEnvironment.idle_action || ''),
    'the stored Environment must be the one the keeper will read',
  ).toEqual('pause');

  const model = String(laneAgent.model || '').trim();
  expect(model, 'the lane-selected Agent must name a model route').not.toEqual('');
  expect(
    await api.listEnvironmentModels(parkEnvironment),
    `the parking Environment must expose the lane's model ${JSON.stringify(model)}`,
  ).toContain(model);

  // ── The conversation a user would leave ──────────────────────────────────
  const agent = await api.createAgent({
    name: `__e2e_idle_park_${runId}`,
    model,
    environment_name: parkEnvironment,
    // No prepared slot: this Agent must not spend its setup warming a box it
    // never claims, and a pooled box is not this conversation's alone.
    prewarm_enabled: false,
    idle_hibernate_seconds: IDLE_WINDOW_SECONDS,
  });
  agentId = String(agent.agent_id || '');
  expect(agentId, 'the Agent must be created').not.toEqual('');
  // `createAgent` projects its payload through the authoring schema and drops
  // anything the schema does not declare. A dropped window is invisible: the
  // keeper would fall back to the 1800s deployment default and judge this Agent
  // by a clock nothing in this spec set, and nothing would park inside the wall.
  expect(
    Number((await api.getAgent(agentId)).idle_hibernate_seconds),
    'the Agent must carry the idle window this journey waits out',
  ).toBe(IDLE_WINDOW_SECONDS);

  const created = await api.startConversation(agentId);
  const sessionId = String(created.session_id || '').trim();
  expect(sessionId, 'starting a conversation must open a session').not.toEqual('');
  sessions.push(sessionId);
  const ready = await api.waitForSessionReady(sessionId, READY_TIMEOUT_MS);
  const boxBefore = String(ready.sandbox_id || '').trim();
  expect(boxBefore, 'a READY conversation must name its box').not.toEqual('');
  test.info().annotations.push({
    type: 'idle_park_scene',
    description: JSON.stringify({
      session: sessionId,
      sandbox: boxBefore,
      agent: agentId,
      environment: parkEnvironment,
      window_seconds: IDLE_WINDOW_SECONDS,
    }),
  });

  // ── The user talks to it once, from the real composer ────────────────────
  await page.setViewportSize({ width: 1440, height: 900 });
  await openSessionView(page, sessionId);
  await expectComposerEnabled(page);
  const firstPrompt = `Idle-park E2E ${runId}. Reply with one short sentence.`;
  const repliesBeforeFirst = await page.getByTestId('assistant-message').count();
  await sendPrompt(page, sessionId, firstPrompt);
  // Count, not wording: the model's phrasing is its own business, and this spec
  // runs under whichever engine the matrix selected.
  await expect
    .poll(() => page.getByTestId('assistant-message').count(), { timeout: REPLY_TIMEOUT_MS })
    .toBeGreaterThan(repliesBeforeFirst);
  const firstReply = page.getByTestId('assistant-message').last();
  await expect(
    firstReply,
    'the pre-park turn must produce a reply, or there is no conversation to come back to',
  ).not.toBeEmpty();
  // A failed turn renders its error INTO the transcript as an assistant message,
  // so "one more non-empty bubble" is satisfied by exactly the outcome a broken
  // box would produce.
  await expect(firstReply).not.toContainText(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);
  await expect(
    page.getByTestId('run-view').getByTestId('status-pill').first(),
    'the pre-park turn must settle before the conversation can be called quiet',
  ).toHaveAttribute('data-pulse', 'false', { timeout: REPLY_TIMEOUT_MS });

  // ── …and leaves a file behind ────────────────────────────────────────────
  // Planted through the API: setup, not journey. Asking the model to write it
  // would make a durability assertion fail whenever the model declined a tool.
  // The workspace root comes from the product, never from a literal — the
  // short-cwd profile can move it.
  const rootListing = await platform.listFiles(sessionId);
  const root = String(rootListing.root_path || '').trim();
  expect(root, 'the files listing must name the workspace root').not.toEqual('');
  const markerName = `idle-park-note-${runId}.md`;
  const markerBody = `left here before the park at ${new Date().toISOString()}\n`;
  await api.uploadFileText(sessionId, root, markerName, markerBody);
  const markerPath = `${root}/${markerName}`;
  expect(
    await api.downloadFileText(sessionId, markerPath),
    'the marker must be in the workspace before the park, or its later absence proves nothing',
  ).toEqual(markerBody);

  // ── The user closes the tab ──────────────────────────────────────────────
  // From here until the return, nothing touches the session over an endpoint
  // that could attach a runtime: every reading is the document store or the
  // operator sandbox read.
  await closePageAfterAssertions(page);

  // ── The conversation goes quiet past the window it was given ─────────────
  const quietDeadline = Date.now() + QUIET_TIMEOUT_MS;
  for (;;) {
    const quiet = quietForMs(sessionId);
    if (quiet.state === 'IDLE' && (quiet.quietMs ?? -1) >= IDLE_WINDOW_MS) break;
    if (Date.now() >= quietDeadline) {
      throw new Error(
        `the conversation must be IDLE past its ${IDLE_WINDOW_SECONDS}s window before the keeper `
          + `can be asked about it, and it was not within ${QUIET_TIMEOUT_MS}ms: `
          + `${JSON.stringify(quiet)}. A conversation that never settles means the first turn is `
          + `still running (session=${sessionId} sandbox=${boxBefore}).`,
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }

  // ── The platform's keeper parks it ───────────────────────────────────────
  // `POST /admin/sandbox-idle-sweep` runs the identical `scan_once()` the 300s
  // timer runs, so this neither re-implements the sweep nor waits out a watcher
  // interval. More than one tick may be needed: the scan takes at most
  // `_IDLE_SWEEP_SCAN_LIMIT` rows per tick with no ordering, so a deployment
  // holding other live conversations can truncate the first tick before it
  // reaches this one.
  const ticks: Array<Record<string, number>> = [];
  const parkDeadline = Date.now() + PARK_SETTLE_MS;
  let parkedAt = '';
  for (;;) {
    ticks.push(sweepCounters(await platform.idleSweep()));
    parkedAt = parkedMark(sessionId);
    if (parkedAt) break;
    if (Date.now() >= parkDeadline) break;
    await new Promise((resolve) => setTimeout(resolve, 3_000));
  }
  const boxAfterPark = String((await api.getSandbox(boxBefore)).state || '').toUpperCase();
  await test.info().attach('idle-sweep-ticks', {
    body: JSON.stringify(
      {
        ticks,
        session: sessionId,
        sandbox: boxBefore,
        sandbox_state: boxAfterPark,
        sandbox_parked_at: parkedAt,
        quiet: quietForMs(sessionId),
        still_a_candidate: disqualifiedFromTheSweep(sessionId) || '<yes>',
      },
      null,
      2,
    ),
    contentType: 'application/json',
  });

  expect(
    ticks.some((tick) => Number(tick.idle_candidates || 0) >= 1) || parkedAt !== '',
    'the keeper never reached this conversation: no tick listed an idle candidate and no tick '
      + `parked it. session=${sessionId} sandbox=${boxBefore} `
      + `disqualified=${disqualifiedFromTheSweep(sessionId) || '<no, it still qualifies>'} `
      + `ticks=${JSON.stringify(ticks)}`,
  ).toBe(true);
  expect(
    parkedAt,
    'the keeper did not park this conversation, so nothing below can be said about coming back '
      + `to a parked one. session=${sessionId} sandbox=${boxBefore} state=${boxAfterPark} `
      + `quiet=${JSON.stringify(quietForMs(sessionId))} `
      + `disqualified=${disqualifiedFromTheSweep(sessionId) || '<no, it still qualifies>'} `
      + `ticks=${JSON.stringify(ticks)}. Either the Environment did not store idle_action `
      + '"pause", or the retention renew did not take, or the snapshot commit did not settle on '
      + 'this cluster (docs/providers/opensandbox.md names what pausing needs).',
  ).not.toEqual('');
  expect(
    [...PAUSED_STATES],
    `the box must be committed, not merely marked: the control plane reads ${boxAfterPark} `
      + `(sandbox=${boxBefore}, session=${sessionId})`,
  ).toContain(boxAfterPark);
  expect(
    String((await api.getSession(sessionId)).sandbox_id || '').trim(),
    'parking is not a replacement: the conversation must still point at the box it filled',
  ).toEqual(boxBefore);

  // ── The user comes back, in a fresh tab ──────────────────────────────────
  // A new page, not a reload of the tab that was left open: returning to a
  // conversation is a cold load, and a live tab carries warm client state the
  // returning user does not have.
  const returning = await context.newPage();
  await returning.setViewportSize({ width: 1440, height: 900 });
  await openSessionView(returning, sessionId);

  // THE RETURN READS CALM. Park writes no `runtime_unavailable` and no state
  // change, so the session still projects READY — and if a fix ever makes the
  // Files panel honest by making the whole conversation look broken, this is
  // what refuses it.
  const pill = returning.getByTestId('run-view').getByTestId('status-pill').first();
  await expect(
    pill,
    'a conversation whose box was parked must still read Ready — the park is the platform\'s '
      + 'business, not the user\'s',
  ).toContainText('Ready', { timeout: 30_000 });
  await expect(pill).not.toContainText('Runtime disconnected');
  await expect(pill).not.toContainText('Recovery needed');
  await expect(returning.getByText('Runtime disconnected')).toHaveCount(0);
  await expect(returning.getByText('Recovery needed')).toHaveCount(0);
  await expect(
    returning.getByRole('button', { name: 'Recover session' }),
    'a parked conversation is not a broken one: nothing may ask the user to recover it',
  ).toHaveCount(0);
  await expectComposerEnabled(returning);
  await expect(
    returning.getByTestId('user-message').filter({ hasText: firstPrompt }).last(),
    'the fresh load must show the message the user sent before leaving',
  ).toBeVisible({ timeout: 30_000 });

  // ── They look at the workspace ───────────────────────────────────────────
  // Files is already the default tab (sessionCapabilities.ts:49); the click
  // states the journey without remounting the panel.
  await returning.getByRole('tab', { name: 'Files', exact: true }).click();
  const filesPanel = returning
    .getByRole('tabpanel')
    .filter({ has: returning.getByText('Current directory', { exact: true }) })
    .first();
  await expect(
    filesPanel,
    'the Files panel must be the one the returning user lands on',
  ).toBeVisible({ timeout: FILE_PANEL_MS });

  // THE ASSERTION THAT IS EXPECTED RED. One verdict function rather than a bare
  // visibility wait, because the failure SHAPE is unknown from reading — a
  // spinner to the control deadline, a fast refusal, or an unexpected answer —
  // and a timeout on a locator would report none of them. The error strip is the
  // panel's `ErrorNote`, which is an `Alert` and so carries role="alert".
  const filesVerdict = async (): Promise<string> => {
    if ((await filesPanel.getByText(markerName, { exact: true }).count()) > 0) {
      return 'the workspace is listed';
    }
    const strip = filesPanel.getByRole('alert');
    if ((await strip.count()) > 0) {
      return `the panel refused: ${(await strip.first().innerText()).replace(/\s+/g, ' ').trim()}`;
    }
    // Each branch is a string only this panel emits, and the console is pinned to
    // en-US above — so a verdict names the state the panel is actually in rather
    // than collapsing three different failures into "it did not appear".
    if ((await filesPanel.getByText('Directory unavailable', { exact: true }).count()) > 0) {
      return 'the panel gave up on the listing without an error: Directory unavailable';
    }
    if ((await filesPanel.getByText('Loading directory…', { exact: true }).count()) > 0) {
      return 'the panel is still loading';
    }
    return 'the panel shows neither the file, an error, nor a loading state';
  };
  await expect
    .poll(filesVerdict, {
      timeout: FILE_PANEL_MS,
      intervals: [1_000, 2_000, 3_000],
      message:
        'THE FINDING: the Files panel does not serve the workspace of a conversation whose box '
        + `the platform parked. session=${sessionId} sandbox=${boxBefore} marker=${markerPath}. `
        + 'The panel is ENABLED over this row — park writes only `sandbox_parked_at` and '
        + '`expires_at` with touch_updated_at=False, so state stays READY, sandbox_id stays set '
        + 'and runtime_unavailable stays false, which is all `isSandboxLiveForSession` '
        + '(frontend/src/utils/format.ts:168-175) and `filesPanelEnabled` '
        + '(useSessionRightPanelState.ts:61) ask for — and nothing on the file path wakes '
        + 'anything: `_wake_parked_sandbox` (runtime_ensure.py:485) has one caller, the turn-prepare '
        + 'path (:448), while `SessionFileService._resolve_session_context` '
        + '(session_file_service.py:521-551) resolves the box by id and connects straight to freed '
        + 'compute. A panel that cannot serve the box must at least say so; enabled and silent is '
        + 'the defect either way.',
    })
    .toEqual('the workspace is listed');

  // ── They send the next message ───────────────────────────────────────────
  const repliesBefore = await returning.getByTestId('assistant-message').count();
  await sendPrompt(
    returning,
    sessionId,
    `Idle-park E2E ${runId}, after the park. Reply with one short sentence.`,
  );
  await expect
    .poll(() => returning.getByTestId('assistant-message').count(), { timeout: REPLY_TIMEOUT_MS })
    .toBeGreaterThan(repliesBefore);
  const secondReply = returning.getByTestId('assistant-message').last();
  await expect(
    secondReply,
    'the message sent after the park must be answered, not left as an empty bubble',
  ).not.toBeEmpty();
  // The same reason as the pre-park turn, and here it is the sharper edge: a wake
  // that failed renders its refusal as an assistant message, which a bare "one
  // more bubble" check would accept as a reply.
  await expect(secondReply).not.toContainText(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);
  await expect(
    pill,
    'the post-park turn must settle like any other',
  ).toHaveAttribute('data-pulse', 'false', { timeout: REPLY_TIMEOUT_MS });

  // ── It continued on the SAME box, not a fresh one ────────────────────────
  // The only assertion that can tell a resume from silent workspace loss: a
  // re-borrow after a resume that did not take produces an equally good-looking
  // reply on a box that has none of these files (runtime_ensure.py:467-483,
  // whose refusal to resume is logged at :524-529).
  const afterTurn = await api.getSession(sessionId);
  expect(
    String(afterTurn.sandbox_id || '').trim(),
    'the turn continued on a DIFFERENT box: the parked one did not resume, so this conversation '
      + `re-borrowed and its workspace is not in the new box. was=${boxBefore} `
      + `now=${String(afterTurn.sandbox_id || '')}`,
  ).toEqual(boxBefore);
  expect(
    await api.downloadFileText(sessionId, markerPath),
    'the file the user left before the park must come back byte for byte — a rebuilt box would '
      + 'have neither these bytes nor this file',
  ).toEqual(markerBody);
  await expect(
    filesPanel.getByText(markerName, { exact: true }),
    'the Files panel must still be serving the same workspace once the box is awake',
  ).toBeVisible({ timeout: FILE_PANEL_MS });

  // ── And the conversation is parkable again ───────────────────────────────
  // Both ways out of a parked row clear the mark — a resume that took
  // (runtime_ensure.py:544-551) and the re-borrow that replaces the box
  // (:700) — so this says nothing about which one happened; the same-box
  // assertion above is what separates them. What it does say is that the row
  // did not keep a mark neither path cleared, which would exclude this
  // conversation from the sweep for good
  // (`list_idle_reclaim_candidates` excludes marked rows).
  await expect
    .poll(() => parkedMark(sessionId), {
      timeout: 30_000,
      intervals: [1_000, 2_000],
      message:
        'the conversation still calls itself parked after a turn woke it, so it is excluded from '
        + `the idle sweep forever and its box will never be parked again (session=${sessionId})`,
    })
    .toEqual('');
});
