/**
 * E2E: an idle conversation does not get to freeze the box its Agent's warm
 * capacity is parked in, and the next person still arrives fast.
 *
 * THE JOURNEY. An operator runs an Agent-tenancy Environment that pauses idle
 * boxes instead of letting them die at their lease. One conversation with that
 * Agent runs a turn and then sits quiet past its idle window — a person went to
 * lunch. Inside the same box, unclaimed, sits the Agent's prepared slot: the
 * engine child that makes the NEXT conversation a 56ms claim instead of a 17s
 * cold start. Nobody is in that conversation, so the keeper parks the box. The
 * next person opens a conversation with the Agent, claims a slot whose manifest
 * still reads `prepared` and whose engine child the pause killed, and pays for
 * a staleness they had no part in.
 *
 * THE WOUND THIS GATES, and it is written into the product as an asymmetry
 * between two functions that ask the same question:
 *
 *   - the DESTROY path, `agent_box_has_other_occupants`
 *     (runtime_manager.py:3182-3260), asks four: bound session rows, in-flight
 *     `startup_allocation` rows, the lease's admissions ledger, and — in its own
 *     words — "a prepared slot occupies the box without being a session at all:
 *     its whole purpose is to exist BEFORE a conversation claims it, so it is
 *     recorded on the Agent row and no session query can see it".
 *   - the PARK path, `_box_is_this_session_s_alone`
 *     (expiration_watcher.py:427-467), asks exactly one:
 *     `list_sessions_by_sandbox_id`. A prepared slot is not a session row. So
 *     the sweep reads the box as this one conversation's alone and
 *     `_park_sandbox` (:358-425) renews it to retention, marks it parked and
 *     pauses the Pod — with the slot inside.
 *
 * And nothing downstream notices. `claim_prepared_slot`
 * (prepared_slots.py:930-1010) admits a manifest on `state == "prepared"` and a
 * matching runtime generation; it never probes the box. The next conversation
 * therefore claims a slot in a paused Pod, and the loss surfaces as a claim that
 * went nowhere with nothing pointing back at the sweep.
 *
 * WHY IT ASSERTS A PROPERTY AND NOT A CALL. Several fixes are honest here:
 * teaching the park predicate to read `_prepared_slot` the way the destroy path
 * does, retiring the slot before the pause and rebuilding it on wake, or
 * refusing the claim on a parked box. All of them leave this spec green, because
 * what it asserts is the outcome — the box stayed live, the slot stayed
 * claimable, and a second person got a working conversation out of it — not
 * which function learned something.
 *
 * WHAT MAKES IT A GATE AND NOT A HOPE. Five ways "the box was not parked" could
 * mean nothing, and what closes each:
 *
 *   - THE KEEPER NEVER RAN. Two arms. The deployed
 *     `ASTRABOX_EXPIRATION_WATCHER_INTERVAL_SECONDS` is read off the server
 *     container and required to be a short positive number, so the quiet wait
 *     provably spans several real ticks; and the spec then drives one tick
 *     itself through `POST /admin/sandbox-idle-sweep`, which runs the server's
 *     own `_expiration_watcher.scan_once()` (sandboxes.py:467-489).
 *   - THE SWEEP NEVER LOOKED AT THIS ROW. The tick's own `idle_candidates` must
 *     be at least one and FEWER than `_IDLE_SWEEP_SCAN_LIMIT`
 *     (expiration_watcher.py:40): a tick under the limit returned every row the
 *     candidate query matched, and the spec separately proves this session's row
 *     matches that query field by field.
 *   - THE ROW WOULD HAVE BEEN SKIPPED ANYWAY. Every enabling condition is
 *     asserted true immediately before the tick: the Environment stores
 *     `idle_action: "pause"`, the Agent carries the window this run waits out,
 *     the snapshot reads IDLE, and the quiet age has passed the window. With
 *     those held, `_idle_window_if_parking` (:482-546) returns a window and
 *     `_is_idle_past` (:549-575) returns true, so control reaches
 *     `_park_sandbox` and the occupancy gate is the only thing left that can
 *     stop it.
 *   - IT WAS ALREADY PARKED. The parked mark is required empty at the moment
 *     each reading is taken. `_park_sandbox` writes that mark BEFORE it starts
 *     the pause commit, precisely so an arriving turn can see the decision, so
 *     the mark is the earliest and cheapest witness that the decision was taken
 *     — and it is polled all through the quiet wait, which is what makes a red
 *     run fail in about half a minute instead of at the lane's wall.
 *   - A COLD START RESCUED THE USER. The second conversation must land on the
 *     SAME box and on the SAME `isolated_session_id` the manifest advertised a
 *     moment earlier. Without that pair this spec would also pass on a
 *     deployment that quietly rebuilt everything, which is the 17s regression
 *     prepared slots exist to prevent.
 *
 * WHAT IS DELIBERATELY NOT ASSERTED. Not `summary.idle_parked === 0`, and no
 * other tick counter, as the verdict. Those counters are deployment-wide: a
 * conversation left behind by another park spec would redden this run for
 * somebody else's row, and a regression that parked THIS box while parking
 * nothing else would still read zero. The per-run verdict is this session's own
 * row, this box's own state, and this Agent's own manifest. The counters are
 * attached to the report as evidence that the tick happened, nothing more.
 *
 * THE PREMISE IS OPERATOR CONFIGURATION, NOT THE LANE DEFAULT. Every Environment
 * the lane deployment provisions stores `idle_action: "terminate"`
 * (configure-credential-environments.py:238), under which
 * `_idle_window_if_parking` returns None and the sweep has no business with any
 * session — the journey would have no configuration in which it could pass or
 * fail. So the spec writes its own Environment through the real admin route,
 * cloned from the deployment's Agent-tenancy prewarm Environment with ONE field
 * changed. Cloning that one rather than the lane Agent's own is what the journey
 * needs and not a preference: `prepare_slot_for_agent` returns None outright for
 * conversation tenancy (prepared_slots.py:620), so a prepared slot only exists
 * inside an Agent-owned shared box, and `astrabox-e2e-prewarm-shared` is the
 * deployment's proven one — Agent tenancy, advanced permission, and
 * deployment model access, so the clone carries no masked secret. The engine is
 * therefore whichever that Environment declares, exactly as for the other
 * prepared-slot specs (ownerless-sandbox-reaping-preserves-prepared-capacity,
 * opensandbox-agent-prewarm-real-extensions), and the spec needs no matrix
 * contract entry of its own.
 *
 * BOTH PRECONDITIONS ARE ASSERTED, NEVER SKIPPED. Agent tenancy is read back off
 * the stored Environment, and the engine's `prepare_runtime` seam is proved by
 * `preparedRuntime(agentId)` actually becoming ready — an adapter that inherits
 * the refusing default yields no slot, and this spec fails loudly rather than
 * passing on a deployment where the scene it describes cannot exist.
 *
 * EXCLUSIVE, AND SERIAL. `scan_once()` sweeps the whole deployment — startup
 * allocations, abandoned Agent boxes, ownerless boxes, prepared-slot renewal,
 * dead bindings, then idle bindings — over every session and every box. Beside
 * parallel workers it would reap and park theirs. And on a red run it starts a
 * real park, whose image commit measured 3m45s under five competing workers
 * (tests/e2e_suite_membership_contract_test.py:123-128). It belongs in the suite
 * contract's one-worker serial group with the other park and sweep specs.
 *
 * WHAT THIS SPEC DOES NOT COVER. The same gate's other blind spot — a second
 * conversation still mid-startup when the tick fires — is out of scope here. The
 * park predicate consults neither `startup_allocation` nor the admissions ledger
 * either, so a joiner is invisible for the whole of its startup and not merely
 * for the ten-second admission window; but reproducing that deterministically
 * needs an interception inside the server's own startup path, and a
 * `docker exec python -c` worker patches a different process. Polling and hoping
 * would be a flaky negative. That belongs in its own spec shaped like
 * ownerless-sandbox-reaping's driver, with a counter proving the race was
 * actually crossed.
 *
 * WHAT A FAILURE LEAVES BEHIND: `trackSessions` keeps both conversations and
 * their box and names them in the report tail. The Agent and the Environment are
 * left in place too; on a pass the Agent is deleted and the Environment is
 * disabled rather than deleted, because the admin route offers environments only
 * GET and PUT.
 */
import { execFileSync } from 'node:child_process';

import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { documentsByField, sessionDoc, snapshotDoc } from '../fixtures/dbOracle';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

// Per-tick parking budget in the product (`_IDLE_SWEEP_SCAN_LIMIT`,
// expiration_watcher.py:40). Mirrored here for one purpose: a tick reporting
// FEWER candidates than this returned every row the candidate query matched, so
// a row that matches the query was necessarily among them. A tick at or above it
// was truncated and can prove nothing about this conversation.
const IDLE_SWEEP_SCAN_LIMIT = 10;

// Upstream's phase vocabulary, carried through by the provider verbatim
// (providers/open_sandbox/sandbox.py:130-131). Membership, not inequality: a box
// reporting PAUSED or SUCCEED must fail this spec rather than merely "differ
// from what we expected".
const RUNNING_BOX_STATES = new Set(['RUNNING', 'READY']);

// The window this Agent asks to be allowed to sit quiet, really waited out
// rather than backdated — the sweep reads a snapshot clock, and a spec that
// wrote that clock itself could not tell the product's silence from its own.
// Tunable so a lane short of wall can shrink it; every assertion reads the
// Agent's STORED value rather than this constant.
const IDLE_WINDOW_MS = parseTimeoutEnv('ASTRABOX_E2E_IDLE_PARK_WINDOW_MS', 15_000);
const IDLE_WINDOW_SECONDS = Math.max(1, Math.round(IDLE_WINDOW_MS / 1_000));

// The pool acquiring a resident box and assembling a slot in it.
const PREPARED_READY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_PREPARED_READY_TIMEOUT_MS', 60_000);
// The one turn that gives the keeper a snapshot clock to read.
const FIRST_TURN_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_IDLE_PARK_TURN_TIMEOUT_MS', 90_000);
// How long the conversation may take to settle IDLE past its window.
const QUIET_TIMEOUT_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_IDLE_PARK_QUIET_TIMEOUT_MS',
  IDLE_WINDOW_MS + 45_000,
);
// The second person's whole arrival: picker, click, conversation, one reply.
const ARRIVAL_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_IDLE_PARK_ARRIVAL_TIMEOUT_MS', 120_000);
const REPLY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_IDLE_PARK_REPLY_TIMEOUT_MS', 90_000);

// A watcher interval this long or longer cannot produce a tick inside the quiet
// wait, so a run on such a deployment would rest on the driven sweep alone and
// must say so rather than quietly narrowing what it proved.
const MAX_USEFUL_WATCHER_INTERVAL_SECONDS = 60;

// Sessions are deleted only when the test passes; a failure keeps both scenes
// and names them in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();
let agentId = '';
let parkEnvironment = '';
let parkPayload: Record<string, unknown> = {};

// Registration order is teardown order: the conversations go first
// (trackSessions, registered above), then the Agent whose box they live in,
// then the Environment nothing references any more.
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});
onPassOnly(async ({ request }) => {
  if (!parkEnvironment) return;
  await new PlatformApi(request).putEnvironment(parkEnvironment, {
    ...parkPayload,
    enabled: false,
  });
});

/** The public runtime coordinates of the Agent's advertised warm capacity. */
interface PreparedCapacity {
  slotId: string;
  state: string;
  sandboxId: string;
  isolatedSessionId: string;
  homeDir: string;
}

/**
 * Read the advertised slot straight out of the Agent document.
 *
 * Preparation also carries credentials, and an assertion retains its expected
 * and actual values in the report, so only public coordinates are projected —
 * the same projection ownerless-sandbox-reaping's `preparedIdentity()` makes.
 */
function preparedCapacity(id: string): PreparedCapacity {
  const row: Record<string, unknown> = documentsByField('agents', '$.agent_id', id)[0] ?? {};
  const manifest = (row._prepared_slot ?? {}) as Record<string, unknown>;
  return {
    slotId: String(manifest.slot_id ?? ''),
    state: String(manifest.state ?? ''),
    sandboxId: String(manifest.sandbox_id ?? ''),
    isolatedSessionId: String(manifest.isolated_session_id ?? ''),
    homeDir: String(manifest.home_dir ?? ''),
  };
}

/** The park DECISION's own mark, written before the pause commit begins. */
function parkedMark(sessionId: string): string {
  return String(sessionDoc(sessionId)?.sandbox_parked_at || '').trim();
}

/** Milliseconds since the snapshot clock the keeper reads last moved, or null. */
function quietFor(sessionId: string): { state: string; quietMs: number | null } {
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
 * Without it, "the box was not parked" is satisfied for free by a row the sweep
 * never looked at — a lapsed lease, a cleared sandbox pointer, a
 * `runtime_unavailable` mark. Mirrors `list_idle_reclaim_candidates`
 * (session_repository.py:536-549); the failure names the field that disqualified
 * the row rather than reporting a vague "not a candidate".
 */
function disqualifiedFromTheSweep(sessionId: string, expectedSandbox: string): string {
  const row = sessionDoc(sessionId);
  if (!row) return 'the session row is gone';
  if (row.deleted === true) return 'the row is marked deleted';
  if (row.runtime_unavailable === true) return 'the row is marked runtime_unavailable';
  const sandboxId = String(row.sandbox_id || '').trim();
  if (!sandboxId) return 'the row no longer names a sandbox';
  if (sandboxId !== expectedSandbox) {
    return `the row moved to sandbox ${sandboxId}, not the box this scene was built in`;
  }
  const state = String(row.state || '');
  if (state === 'TERMINATED' || state === 'DELETED') return `the row reached ${state}`;
  const expiresAt = Date.parse(String(row.expires_at || ''));
  if (!Number.isFinite(expiresAt)) {
    return `the row has no readable expires_at (${String(row.expires_at)})`;
  }
  if (expiresAt <= Date.now()) {
    return `the sandbox lease already lapsed at ${String(row.expires_at)} — the dead-binding `
      + 'sweep owns this row, not the idle sweep';
  }
  return '';
}

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
 * The deployed expiration watcher's tick interval, read from the server rather
 * than assumed.
 *
 * `printenv` exits non-zero when the variable is unset, which `execFileSync`
 * raises. That is the right outcome, but it has to say so in words: "the spec
 * threw" and "this deployment runs the watcher on a period no quiet wait can
 * span" send a reader to different places. Precedent for reaching into the
 * server container for a deployed value:
 * sandbox-oob-death-reborrow.exclusive.spec.ts:66.
 */
function deployedWatcherIntervalSeconds(): number {
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  let raw = '';
  try {
    raw = execFileSync('docker', [
      'exec', server, 'printenv', 'ASTRABOX_EXPIRATION_WATCHER_INTERVAL_SECONDS',
    ], { encoding: 'utf8', timeout: 30_000, stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  } catch (error) {
    throw new Error(
      'the deployed server states no ASTRABOX_EXPIRATION_WATCHER_INTERVAL_SECONDS, so this run '
        + 'cannot say how many real keeper ticks its quiet wait spans, and "the box survived" '
        + 'would rest on the one sweep this spec drives by hand.\n'
        + `${error instanceof Error ? error.message : String(error)}`,
    );
  }
  const seconds = Number(raw);
  expect(
    Number.isFinite(seconds) && seconds > 0,
    `the deployed watcher interval must be a positive number of seconds; got ${JSON.stringify(raw)}`,
  ).toBe(true);
  return seconds;
}

test('an idle conversation does not park a box holding the Agent\'s prepared slot, and the next conversation claims it and answers', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const runId = new Date().toISOString().replace(/[:.]/g, '-');
  const milestones: Array<Record<string, unknown>> = [];
  const startedAt = Date.now();
  const mark = (what: string, detail: Record<string, unknown> = {}): void => {
    milestones.push({ what, at_ms: Date.now() - startedAt, ...detail });
  };

  // ── PRECONDITION: this deployment can park at all ────────────────────────
  const capability = await platform.idleAction();
  expect(
    ((capability.supported_actions as unknown[] | undefined) || []).map((action) => String(action)),
    'this deployment\'s sandbox backend cannot snapshot, so `idle_action: "pause"` is refused at '
      + 'the Environment form and this journey has no configuration in which it can pass or fail. '
      + `The route's own reason: ${String(capability.detail ?? '<none given>')} `
      + `(backend=${String(capability.backend ?? '<unnamed>')})`,
  ).toContain('pause');

  // ── PRECONDITION: a keeper that actually ticks ───────────────────────────
  const watcherIntervalSeconds = deployedWatcherIntervalSeconds();
  expect(
    watcherIntervalSeconds,
    'this deployment runs the expiration watcher on a period longer than any quiet wait a 180s '
      + 'spec can afford, so the production loop could not be said to have judged this box and '
      + 'the run would rest on the single tick this spec drives by hand. Shorten '
      + 'ASTRABOX_EXPIRATION_WATCHER_INTERVAL_SECONDS on the testbed.',
  ).toBeLessThanOrEqual(MAX_USEFUL_WATCHER_INTERVAL_SECONDS);
  mark('watcher_interval_read', { watcherIntervalSeconds });

  // ── An Environment that parks, over the box shape a slot can live in ─────
  // Cloned from the deployment's proven Agent-tenancy prewarm Environment: a
  // prepared slot exists only inside an Agent-owned shared box
  // (prepared_slots.py:620), and this is the Environment the other prepared-slot
  // specs are built on. The only changed field is the one the journey rests on.
  const sourceName = String(process.env.ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT || '').trim();
  expect(
    sourceName,
    'ASTRABOX_E2E_PREWARM_SHARED_ENVIRONMENT names the deployment\'s Agent-tenancy prewarm '
      + 'Environment and the lane must export it; this spec reads it and never writes it',
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
  // is added to the first, and projecting also drops computed reads the
  // validator would reject.
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
  parkEnvironment = `__e2e-idle-park-slot-${sourceName}`;
  parkPayload = {
    ...Object.fromEntries(
      editableKeys
        .filter((key) => key in sourceEnvironment)
        .map((key) => [key, sourceEnvironment[key]]),
    ),
    display_name: 'Idle parking over a prepared slot (E2E)',
    description: `Created by ${test.info().titlePath.join(' > ')}.`,
    enabled: true,
    // The keeper only has business with a conversation whose Environment says
    // pause; under terminate `_idle_window_if_parking` returns None.
    idle_action: 'pause',
  };
  let storedEnvironment: Record<string, unknown>;
  try {
    storedEnvironment = await platform.putEnvironment(parkEnvironment, parkPayload);
  } catch (error) {
    parkEnvironment = '';
    throw new Error(
      'this deployment refused an Environment that parks idle boxes, so the journey has no '
        + 'configuration in which it can pass or fail. Under idle_action "terminate" the keeper '
        + 'leaves every box alone and the box dies at its lease instead, which no 180s spec can '
        + `reach.\n${error instanceof Error ? error.message : String(error)}`,
    );
  }
  expect(
    String(storedEnvironment.idle_action || ''),
    'the stored Environment must be the one the keeper will read',
  ).toEqual('pause');
  expect(
    String(storedEnvironment.sandbox_tenancy || ''),
    'the box under test must be the Agent\'s, carrying its conversations as isolated sessions: '
      + '`prepare_slot_for_agent` returns None outright for conversation tenancy '
      + '(prepared_slots.py:620), so under any other tenancy there is no prepared slot inside a '
      + 'conversation\'s box and this journey has no scene',
  ).toEqual('agent');
  expect(
    String(storedEnvironment.sandbox_permission_level || ''),
    'Agent tenancy carves each conversation a namespace inside one box, which the write path '
      + 'admits only at an elevated permission level (environment_schema.py:203-211)',
  ).toEqual('advanced');
  expect(
    storedEnvironment.enabled,
    'a disabled Environment cannot run the Agent this spec creates on it',
  ).not.toBe(false);
  mark('park_environment_stored', { parkEnvironment });

  // ── An Agent that prewarms, and is allowed to sit quiet for the window ───
  const researchAgent = String(process.env.ASTRABOX_E2E_RESEARCH_AGENT || '').trim();
  expect(
    researchAgent,
    'ASTRABOX_E2E_RESEARCH_AGENT names a deployment-proven Agent whose model route this spec '
      + 'borrows; the lane must export it',
  ).not.toEqual('');
  const model = await api.configuredAgentModel(researchAgent, parkEnvironment);
  const agentName = `__e2e_idle_park_slot_${runId}`;
  const created = await api.createAgent({
    name: agentName,
    model,
    environment_name: parkEnvironment,
    // The whole point of the scene: warm capacity parked in the box alongside
    // the one conversation that is about to go quiet.
    prewarm_enabled: true,
    idle_hibernate_seconds: IDLE_WINDOW_SECONDS,
  });
  agentId = String(created.agent_id || '');
  expect(agentId, 'the Agent must be created').not.toEqual('');
  // `createAgent` projects its payload through the authoring schema and drops
  // anything the schema does not declare. A dropped window is invisible: the
  // sweep would fall back to the 1800s deployment default
  // (`_idle_window_if_parking`, expiration_watcher.py:538-546) and judge this
  // Agent by a clock this spec never set, and nothing would be asked inside this
  // run's wall.
  const readBack = await api.getAgent(agentId);
  expect(
    Number(readBack.idle_hibernate_seconds),
    'the Agent must carry the idle window this run waits out',
  ).toBe(IDLE_WINDOW_SECONDS);
  expect(
    readBack.prewarm_enabled,
    'the Agent must carry the prewarm setting whose slot this journey protects',
  ).toBe(true);
  mark('agent_created', { agentId, agentName });

  /** Poll the Agent's advertised capacity until the platform says it is claimable. */
  const advertisedCapacity = async (why: string): Promise<Record<string, unknown>> => {
    let last: Record<string, unknown> = {};
    await expect.poll(async () => {
      last = await platform.preparedRuntime(agentId);
      return Boolean(last.ready && last.sandbox_id);
    }, {
      timeout: PREPARED_READY_TIMEOUT_MS,
      intervals: [1_000, 2_000],
      message:
        `${why}: this Agent must publish real claimable capacity. A slot that never becomes `
        + 'ready means either the pool could not hand out a resident box, or this engine has no '
        + '`prepare_runtime` seam and inherits the refusing default — in which case the scene '
        + 'this spec describes cannot exist on this deployment and the run is refused rather '
        + `than passed. Last readout: ${JSON.stringify(last)}`,
    }).toBe(true);
    return last;
  };

  // ── The conversation the person leaves quiet ─────────────────────────────
  const first = await api.startConversation(agentId);
  const idleSession = String(first.session_id || '').trim();
  expect(idleSession, 'starting a conversation must open a session').not.toEqual('');
  sessions.push(idleSession);
  const ready = await api.waitForSessionReady(idleSession);
  const box = String(ready.sandbox_id || '').trim();
  expect(box, 'a READY conversation must name the Agent\'s resident box').not.toEqual('');
  mark('idle_conversation_ready', { idleSession, box });

  // One real turn, and it is load-bearing twice. `_is_idle_past` reads the
  // session SNAPSHOT, which exists only once turn activity wrote one, so without
  // it the sweep would refuse for a reason that has nothing to do with this
  // journey and the run would be a false green. It is also what makes the
  // platform refill the slot this conversation just claimed.
  const turn = await api.sendTurn(
    idleSession,
    `E2E idle park ${runId}. Reply with the single word READY and nothing else.`,
    FIRST_TURN_TIMEOUT_MS,
  );
  expect(
    turn.errorText,
    'the first conversation must complete one ordinary turn; without it there is no snapshot '
      + 'clock for the keeper to read and the sweep would refuse for the wrong reason',
  ).toBeNull();
  mark('first_turn_done', { totalMs: turn.totalMs });

  // ── The spare slot the next person is going to claim ─────────────────────
  const advertised = await advertisedCapacity('after the first conversation claimed its slot');
  expect(
    String(advertised.sandbox_id || ''),
    'the refilled slot must sit inside the SAME box the idle conversation holds — that is the '
      + 'whole scene: one bound conversation plus one unclaimed prepared slot, one box. A slot '
      + 'in a different box would make the keeper right to park this one',
  ).toEqual(box);
  const manifestBefore = preparedCapacity(agentId);
  expect(manifestBefore.state, 'the advertised slot must read prepared').toEqual('prepared');
  expect(manifestBefore.sandboxId, 'the advertised slot must name the shared box').toEqual(box);
  expect(
    manifestBefore.isolatedSessionId,
    'the advertised slot must name the engine child a claim adopts',
  ).not.toEqual('');
  // The scene must still be unparked at the moment it is finished being built.
  // A box already parked during the setup would make every later reading a
  // reading of the aftermath, and "it was already parked" would be
  // indistinguishable from "the keeper refused". Named here so a park that
  // landed during the slot refill reports itself rather than surfacing as a
  // prewarm timeout.
  expect(
    parkedMark(idleSession),
    'the keeper paused this box while the scene was still being built — before the conversation '
      + `had even reached its idle window. session=${idleSession} sandbox=${box} `
      + `slot=${JSON.stringify(manifestBefore)}`,
  ).toEqual('');
  test.info().annotations.push({
    type: 'idle_park_scene',
    description: JSON.stringify({
      idleSession, box, agentId, parkEnvironment,
      window_seconds: IDLE_WINDOW_SECONDS, watcherIntervalSeconds,
      expected_production_ticks: Math.floor((IDLE_WINDOW_MS / 1_000) / watcherIntervalSeconds),
      slot: manifestBefore,
    }),
  });
  mark('slot_refilled', { slot: manifestBefore.slotId });

  // ── The person goes quiet ────────────────────────────────────────────────
  // Really waited, never backdated. From here nothing touches the conversation
  // over HTTP: a read through an endpoint that ensures or attaches a runtime
  // would be this spec performing the presence it is trying to observe. Every
  // reading below the wait comes from the document store or the control plane.
  //
  // The parked mark is polled all the way through, so on a product where
  // the production loop parks this box within a couple of its own ticks — the
  // run fails here, in about half a minute, naming the defect, instead of at the
  // lane's wall on a browser step that never had a chance.
  const quietDeadline = Date.now() + QUIET_TIMEOUT_MS;
  for (;;) {
    const quiet = quietFor(idleSession);
    expect(
      parkedMark(idleSession),
      'THE FINDING (during the wait): the keeper paused a box that was holding this Agent\'s '
        + `unclaimed prepared slot. session=${idleSession} sandbox=${box} `
        + `slot=${JSON.stringify(manifestBefore)} quiet=${quiet.quietMs}ms `
        + `window=${IDLE_WINDOW_SECONDS}s. `
        + '`_box_is_this_session_s_alone` (expiration_watcher.py:427) asks only '
        + '`list_sessions_by_sandbox_id`, and a prepared slot is not a session row — while the '
        + 'destroy path\'s `agent_box_has_other_occupants` (runtime_manager.py:3182) reads the '
        + 'Agent row\'s `_prepared_slot` for exactly this reason. The next person now claims a '
        + 'slot whose engine child the pause killed.',
    ).toEqual('');
    if (quiet.state === 'IDLE' && (quiet.quietMs ?? -1) >= IDLE_WINDOW_MS) break;
    if (Date.now() >= quietDeadline) {
      throw new Error(
        `the conversation must be IDLE past its ${IDLE_WINDOW_SECONDS}s window before the keeper `
          + `can be asked about it, and it was not within ${QUIET_TIMEOUT_MS}ms: `
          + `${JSON.stringify(quiet)}. A conversation that never settles means its turn is still `
          + 'running, or something is writing the snapshot clock that this spec did not ask to.',
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }
  mark('quiet_past_window', quietFor(idleSession));

  // ── Every enabling condition, immediately before the tick ────────────────
  // A refusal is indistinguishable from an ineligible candidate unless all of
  // these hold. With them held, `_idle_window_if_parking` returns a window and
  // `_is_idle_past` returns true, so control reaches `_park_sandbox` and the
  // occupancy gate is the only thing left that can stop the park.
  const disqualified = disqualifiedFromTheSweep(idleSession, box);
  expect(
    disqualified,
    'the conversation stopped qualifying for the idle sweep, so its box surviving proves '
      + `nothing: ${disqualified} (session=${idleSession} sandbox=${box})`,
  ).toEqual('');
  const quietAtTick = quietFor(idleSession);
  expect(quietAtTick.state, 'the snapshot must read IDLE when the keeper is asked').toEqual('IDLE');
  expect(
    quietAtTick.quietMs ?? -1,
    'the quiet age must have passed the Agent\'s window when the keeper is asked',
  ).toBeGreaterThanOrEqual(IDLE_WINDOW_MS);
  expect(
    preparedCapacity(agentId),
    'the unclaimed slot must still be in the box when the keeper is asked, or there is nothing '
      + 'for the keeper to be wrong about',
  ).toEqual(manifestBefore);

  // ── The platform's own keeper runs over this box ─────────────────────────
  // `POST /admin/sandbox-idle-sweep` runs the identical `scan_once()` the timer
  // runs (sandboxes.py:467-489), so the spec neither re-implements the sweep nor
  // depends on catching a tick.
  const tick = sweepCounters(await platform.idleSweep());
  mark('sweep_driven', { tick });
  await test.info().attach('idle-sweep-tick', {
    body: JSON.stringify({
      tick,
      session: idleSession,
      sandbox: box,
      parked_at: parkedMark(idleSession),
      quiet: quietFor(idleSession),
      // Deployment-wide counters, attached as evidence that a tick happened —
      // never read as this run's verdict. See the header.
    }, null, 2),
    contentType: 'application/json',
  });
  const candidates = Number(tick.idle_candidates || 0);
  expect(
    candidates,
    'the keeper found no idle candidates at all, so this run cannot claim it judged anything: '
      + `session=${idleSession} was IDLE for ${quietAtTick.quietMs}ms past a ${IDLE_WINDOW_SECONDS}s `
      + `window with a live lease when the tick ran. tick=${JSON.stringify(tick)}`,
  ).toBeGreaterThanOrEqual(1);
  expect(
    candidates,
    `the tick reported ${candidates} idle candidates, at or above \`_IDLE_SWEEP_SCAN_LIMIT\` `
      + `(${IDLE_SWEEP_SCAN_LIMIT}), which means the scan was truncated and cannot be said to have `
      + 'reached this conversation. This deployment is holding too many live conversations for '
      + `the gate to mean anything — tick=${JSON.stringify(tick)}`,
  ).toBeLessThan(IDLE_SWEEP_SCAN_LIMIT);

  // ── THE VERDICT, read three ways off this run's own objects ──────────────
  expect(
    parkedMark(idleSession),
    'THE FINDING: the keeper paused a box that was holding this Agent\'s unclaimed prepared '
      + `slot. session=${idleSession} sandbox=${box} slot=${JSON.stringify(manifestBefore)} `
      + `tick=${JSON.stringify(tick)}. \`_box_is_this_session_s_alone\` `
      + '(expiration_watcher.py:427) asks only `list_sessions_by_sandbox_id`, and a prepared slot '
      + 'is not a session row — while the destroy path\'s `agent_box_has_other_occupants` '
      + '(runtime_manager.py:3182) reads the Agent row\'s `_prepared_slot` for exactly this '
      + 'reason.',
  ).toEqual('');
  const boxState = String((await api.getSandbox(box)).state || '').toUpperCase();
  expect(
    RUNNING_BOX_STATES.has(boxState),
    `the box holding the prepared slot must still be running; the control plane reports `
      + `${JSON.stringify(boxState)} (a paused box reports PAUSED or SUCCEED). session=${idleSession} `
      + `sandbox=${box}`,
  ).toBe(true);
  expect(
    preparedCapacity(agentId),
    'the unclaimed slot must have survived the tick unchanged, in the same box and still '
      + 'reading prepared',
  ).toEqual(manifestBefore);
  mark('verdict_taken', { boxState });

  // ── THE PROPERTY: the next person arrives and gets a conversation ────────
  // This is the assertion that would still fail if the box were paused while
  // every flag happened to read right — the shared-box defects in this repo have
  // repeatedly reported READY on a conversation that could not talk. Read
  // immediately before the click, because a legitimate fix may renew the slot
  // and swap it for a fresh one, and what the claim must match is what was
  // advertised at the moment the person arrived.
  const capacityAtClick = preparedCapacity(agentId);
  expect(
    capacityAtClick.state,
    'the Agent must still advertise a prepared slot when the next person arrives',
  ).toEqual('prepared');
  expect(
    capacityAtClick.sandboxId,
    'the advertised slot must still name the box the keeper was asked about',
  ).toEqual(box);

  await page.goto(appPath('/agents'));
  const card = page.locator(`[data-testid="agent-option"][data-agent-name="${agentName}"]`);
  await expect(
    card,
    'the prewarmed Agent must be offered on the picker the next person uses',
  ).toBeVisible({ timeout: 30_000 });
  // The real entry point. One button per card — the badge, title and model line
  // are all spans — so the button is addressed by role and no localized string
  // is read anywhere in this spec, which is why it pins no locale.
  await card.getByRole('button').click();
  await page.waitForURL((url) => /\/sessions\/[^/]+$/.test(url.pathname), {
    timeout: ARRIVAL_TIMEOUT_MS,
  });
  const nextSession = new URL(page.url()).pathname.split('/').filter(Boolean).pop() || '';
  expect(
    nextSession,
    'starting a conversation must open its own /sessions/<id> route',
  ).not.toEqual('');
  sessions.push(nextSession);
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: ARRIVAL_TIMEOUT_MS });
  mark('next_conversation_opened', { nextSession });

  const composer = page.getByTestId('composer-prompt');
  await expect(
    composer,
    'the next person must be able to type: a conversation that opened on a slot inside a paused '
      + 'box reads ready and cannot talk',
  ).toBeEnabled({ timeout: ARRIVAL_TIMEOUT_MS });
  const repliesBefore = await page.getByTestId('assistant-message').count();
  await composer.fill(`E2E idle park ${runId} second person. Reply with one short sentence.`);
  await page.getByTestId('composer-submit').click();
  await expect(
    page.getByTestId('assistant-message'),
    'the second conversation must actually answer. A claim that lands on an engine child the '
      + `pause killed opens a conversation that never starts. session=${nextSession} sandbox=${box}`,
  ).toHaveCount(repliesBefore + 1, { timeout: REPLY_TIMEOUT_MS });
  mark('next_conversation_answered');

  // ── And it was the prepared slot that served them ────────────────────────
  // Without this pair the spec would also pass on a deployment that quietly cold
  // started a new box — the 17s regression prepared slots exist to prevent.
  const detail = await api.adminSessionDetail(nextSession);
  expect(
    String(detail.sandbox_id || ''),
    'the next conversation must have landed in the Agent\'s resident box, not in a box built for '
      + 'it from cold',
  ).toEqual(box);
  const identity = (detail.runtime_identity ?? {}) as Record<string, unknown>;
  expect(
    String(identity.isolated_session_id || ''),
    'the next conversation must have adopted the engine child the Agent advertised a moment '
      + `earlier (slot=${capacityAtClick.slotId}); a different one means the slot was rebuilt `
      + 'between the readout and the claim, and the claim this journey is about did not happen',
  ).toEqual(capacityAtClick.isolatedSessionId);
  mark('claim_confirmed');

  await test.info().attach('idle-park-milestones', {
    body: JSON.stringify({ runId, agentId, box, milestones }, null, 2),
    contentType: 'application/json',
  });
});
