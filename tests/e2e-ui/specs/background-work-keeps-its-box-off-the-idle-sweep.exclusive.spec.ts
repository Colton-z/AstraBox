/**
 * A held native child survives idle reclamation while an idle control actually pauses.
 *
 * One conversation launches a Claude Code background Agent whose Bash command
 * writes a heartbeat every second until this spec releases it; the reply that
 * launched it ends and the user leaves. A second conversation of the same
 * Agent, in the same Environment, does nothing at all. Both are left alone past
 * the Agent's idle window, then the idle sweep runs
 * (`POST /admin/sandbox-idle-sweep`, the tick `expiration_watcher.py` runs on a
 * timer).
 *
 * The quiet conversation's box must be paused: that is the control proving the
 * sweep parks this Agent's boxes at all. The working one's must not be: its
 * sandbox stays RUNNING and the child's heartbeat keeps advancing after the
 * sweep. Released, the child finishes its command in its own transcript, its
 * run closes with Claude's own completed status, and the conversation comes to
 * rest.
 *
 * The sweep pauses a box when the runtime's last reported status
 * (`sessions.runtime_status`) is idle and the conversation has had no output,
 * activity or resume for the window (`_is_idle_past`). The runtime contract
 * counts background work the runtime started itself as `working`
 * (`RuntimeStatus` in `engine/runtime.py`), so the child keeps its box through
 * that status or through output inside the window, and a pause in between
 * kills it mid-flight.
 */
import { expect, test } from '@playwright/test';
import type { Page, Response } from '@playwright/test';

import { AstraApi, type ChildRunRecord, type SessionRecord } from '../fixtures/astraApi';
import { nativeTranscriptRows, postgresRows, sessionResponseRows, sqlLiteral } from '../fixtures/dbOracle';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { expectComposerEnabled, openSessionView, sendPrompt } from '../fixtures/sessionPage';
test.use({ locale: 'en-US' });
const ENV_API_KEY_SENTINEL = '••••••••';
const IDLE_SWEEP_SCAN_LIMIT = 10;
const IDLE_WINDOW_MS = parseTimeoutEnv('ASTRABOX_E2E_IDLE_WINDOW_MS', 20_000);
const IDLE_WINDOW_SECONDS = Math.ceil(IDLE_WINDOW_MS / 1_000);

const READY_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_READY_TIMEOUT_MS', 90_000);
const BACKGROUND_PROBE_TIMEOUT_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_BACKGROUND_IDLE_PROBE_TIMEOUT_MS',
  60_000,
);
const QUIET_TIMEOUT_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_IDLE_QUIET_TIMEOUT_MS',
  IDLE_WINDOW_SECONDS * 1_000 + 45_000,
);
const CHILD_SETTLED_TIMEOUT_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_BACKGROUND_IDLE_SETTLED_TIMEOUT_MS',
  60_000,
);
const CHILD_DEADLINE_SECONDS = 150;
// Claude's Bash tool stops a command at its timeout; the held command must
// outlive the whole idle journey, so the child asks for the tool's maximum.
const CHILD_BASH_TIMEOUT_MS = 600_000;
const SWEEP_ATTEMPTS = 3;
// Claude Code keeps each Agent's conversation in a transcript of its own,
// `subagents/agent-<id>` in the session's SessionStore mirror
// (`engine/claude_code_background.py`).
const AGENT_TRANSCRIPT_PREFIX = 'subagents/agent-';
const sessions = trackSessions();
let agentId = '';
let parkingEnvironment = '';
let parkingEnvironmentPayload: Record<string, unknown> = {};
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
 * What the idle sweep reads about one conversation, from the `sessions` row
 * itself: the candidate filter (`list_idle_reclaim_candidates`), the runtime's
 * own last status, and how long the conversation has been quiet by the
 * timestamps `_is_idle_past` compares, measured on the database clock.
 *
 * `quiet_ms` is the sweep's whole reading: output, activity (a message sent,
 * a response ended) and a resume of the box. `activity_quiet_ms` leaves out
 * output, which a working runtime itself produces: it says how long nobody has
 * done anything in the conversation.
 */
interface SweepRow {
  state: string;
  deleted: boolean;
  runtime_unavailable: boolean;
  sandbox_id: string | null;
  expires_at: string | null;
  lease_lapsed: boolean;
  sandbox_parked_at: string | null;
  runtime_status: string | null;
  last_output_at: string | null;
  quiet_ms: number | null;
  activity_quiet_ms: number | null;
}

function sweepRow(sessionId: string): SweepRow | null {
  return postgresRows<SweepRow>(
    'SELECT state, deleted, runtime_unavailable, sandbox_id, expires_at, '
      + '(expires_at IS NOT NULL AND expires_at <= now()) AS lease_lapsed, sandbox_parked_at, '
      + 'runtime_status, last_output_at, '
      + '(EXTRACT(EPOCH FROM (now() - GREATEST(last_output_at, updated_at, sandbox_resumed_at))) '
      + '* 1000)::bigint AS quiet_ms, '
      + '(EXTRACT(EPOCH FROM (now() - GREATEST(updated_at, sandbox_resumed_at))) * 1000)::bigint '
      + 'AS activity_quiet_ms '
      + `FROM sessions WHERE session_id = ${sqlLiteral(sessionId)}`,
  )[0] ?? null;
}

function parkedMark(sessionId: string): string {
  return String(sweepRow(sessionId)?.sandbox_parked_at || '').trim();
}

/** Quiet by the sweep's own reading: the runtime reports nothing running, and nothing happened for the window. */
function quietForTheSweep(row: SweepRow | null): boolean {
  return Boolean(row)
    && ['', 'idle'].includes(String(row!.runtime_status ?? ''))
    && (row!.quiet_ms ?? -1) >= IDLE_WINDOW_MS;
}

/** Nothing of the person's is running or waiting: no response, no held input, no question. */
function atRestForItsOwner(detail: SessionRecord): boolean {
  return (detail.current_responses ?? []).length === 0
    && (detail.held_inputs ?? []).length === 0
    && !detail.pending_interaction;
}

function disqualifiedFromTheSweep(sessionId: string): string {
  const row = sweepRow(sessionId);
  if (!row) return 'the session row is gone';
  if (row.deleted === true) return 'the row is marked deleted';
  if (row.runtime_unavailable === true) return 'the row is marked runtime_unavailable';
  if (!String(row.sandbox_id || '').trim()) return 'the row no longer names a sandbox';
  const state = String(row.state || '');
  if (state === 'TERMINATED' || state === 'DELETED') return `the row reached ${state}`;
  if (!row.expires_at) return 'the row has no expires_at';
  if (row.lease_lapsed) {
    return `the sandbox lease already lapsed at ${String(row.expires_at)} — the dead-binding sweep owns this row, not the idle sweep`;
  }
  return '';
}

function contentBlocks(entry: Record<string, unknown>): Record<string, unknown>[] {
  const message = entry.message;
  if (!message || typeof message !== 'object') return [];
  const content = (message as Record<string, unknown>).content;
  return Array.isArray(content)
    ? content.filter((block): block is Record<string, unknown> => Boolean(block) && typeof block === 'object')
    : [];
}

interface ChildBashEvidence {
  input: Record<string, unknown>;
  /** The tool_result blocks Claude recorded for that call, in transcript order. */
  results: Record<string, unknown>[];
}

/**
 * The child's own Bash call for `commandFragment`, read from its transcript in
 * the SessionStore mirror — the supplier's record of whether the held command
 * is still running, independent of the child-run projection and of the sweep.
 */
function childBash(sessionId: string, commandFragment: string): ChildBashEvidence[] {
  const entries = nativeTranscriptRows(sessionId)
    .filter((row) => String(row.subpath ?? '').startsWith(AGENT_TRANSCRIPT_PREFIX))
    .map((row) => JSON.parse(String(row.entry_json)) as Record<string, unknown>);
  const blocks = entries.flatMap((entry) => contentBlocks(entry));
  const calls = new Map<string, Record<string, unknown>>();
  for (const block of blocks) {
    if (block.type !== 'tool_use' || block.name !== 'Bash') continue;
    if (!JSON.stringify(block.input ?? null).includes(commandFragment)) continue;
    calls.set(String(block.id ?? ''), (block.input ?? {}) as Record<string, unknown>);
  }
  return [...calls].filter(([id]) => id).map(([id, input]) => ({
    input,
    results: blocks.filter((block) => block.type === 'tool_result' && block.tool_use_id === id),
  }));
}

/** The input id the inputs route answered a composer send with. */
async function acceptedInputId(response: Response): Promise<string> {
  const body = (await response.json()) as { data?: { input_id?: unknown } };
  const inputId = String(body.data?.input_id ?? '').trim();
  expect(inputId, 'the inputs route must answer with the accepted input id').not.toBe('');
  return inputId;
}

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
  const schema = await platform.environmentSchema();
  const editableKeys = (schema.fields || [])
    .map((field) => String(field.key || ''))
    .filter((key) => key && key !== 'name');
  expect(editableKeys.length, 'the environment schema must declare its editable fields').toBeGreaterThan(0);
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
    idle_action: 'pause',
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
    prepared_capacity: 0,
    idle_hibernate_seconds: IDLE_WINDOW_SECONDS,
  });
  agentId = String(agent.agent_id || '');
  expect(agentId, 'the Agent must be created').not.toEqual('');
  expect(
    Number((await api.getAgent(agentId)).idle_hibernate_seconds),
    'the Agent must carry the idle window this journey waits out',
  ).toBe(IDLE_WINDOW_SECONDS);
  const subjectCreated = await api.startConversation(agentId);
  const subject = String(subjectCreated.session_id || '').trim();
  expect(subject, 'starting a conversation must open a session').not.toEqual('');
  sessions.push(subject);
  const subjectReady = await api.waitForSessionReady(subject, READY_TIMEOUT_MS);
  const subjectSandbox = String(subjectReady.sandbox_id || '').trim();
  expect(subjectSandbox, 'a READY conversation must name its box').not.toEqual('');
  await api.setPermissionMode(subject, 'bypassPermissions');
  const rootListing = await platform.listFiles(subject);
  const root = String(rootListing.root_path || '').trim();
  expect(root, 'the files listing must name the workspace root the child writes into').not.toEqual('');
  const heartbeatName = `.astrabox-bg-heartbeat-${runId}.log`;
  const releaseName = `.astrabox-bg-release-${runId}`;
  const heartbeatPath = `${root}/${heartbeatName}`;
  const releasePath = `${root}/${releaseName}`;
  // Zero until the child's command creates the file: `expect.poll` fails at
  // once on a throwing read, so a missing file is an answer, not an error.
  const heartbeats = async (): Promise<number> => {
    const listing = await platform.listFiles(subject, root, 15_000);
    if (!listing.entries?.some((entry) => entry.name === heartbeatName && entry.kind === 'file')) return 0;
    return (await api.downloadFileText(subject, heartbeatPath)).trim().split('\n').filter(Boolean).length;
  };
  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('astrabox-lang', 'en');
    } catch {

    }
  });
  await openSessionView(page, subject);
  await expectComposerEnabled(page);
  const prompt = [
    'Launch exactly one Agent with run_in_background=true.',
    `The child must use Bash exactly once, with run_in_background=false and timeout=${CHILD_BASH_TIMEOUT_MS}, `
      + 'to run this exact command and wait for it:',
    heartbeatProgram(heartbeatPath, releasePath, doneMarker),
    `The child must report ${doneMarker} verbatim after the command completes.`,
    `Immediately after launching, reply only ${parentMarker} verbatim and end your turn.`,
    'Do not wait for the child and do not run Bash in the parent.',
  ].join('\n');
  const launchInputId = await acceptedInputId(await sendPrompt(page, subject, prompt));
  const controlCreated = await api.startConversation(agentId);
  const control = String(controlCreated.session_id || '').trim();
  expect(control, 'the control conversation must open a session').not.toEqual('');
  sessions.push(control);

  // The child runs before the idle window starts: the platform tracks one
  // Claude Agent the engine reads as working, the child's own transcript holds
  // its held Bash call, and the command is really writing heartbeats.
  const workingChildren = async (): Promise<ChildRunRecord[]> => (await api.listChildRuns(subject)).child_runs
    .filter((row) => row.engine_kind === 'claude_code' && !row.closed && row.active);
  await expect.poll(async () => (await workingChildren()).length, {
    timeout: BACKGROUND_PROBE_TIMEOUT_MS,
    message: 'the launch must leave exactly one Claude background Agent working before the idle window',
  }).toBe(1);
  const childRunId = (await workingChildren())[0]!.child_run_id;
  await expect.poll(() => childBash(subject, heartbeatPath).length, {
    timeout: BACKGROUND_PROBE_TIMEOUT_MS,
    message: 'the background Agent must record its one heartbeat Bash call in its own transcript',
  }).toBe(1);
  expect(
    childBash(subject, heartbeatPath)[0]!.input.run_in_background,
    'the held command must run in the child\'s foreground, so its result stays owed until release',
  ).not.toBe(true);
  await expect.poll(heartbeats, {
    timeout: BACKGROUND_PROBE_TIMEOUT_MS,
    message: `the background Agent's command must start writing ${heartbeatPath}`,
  }).toBeGreaterThan(0);
  const launchReply = await api.waitForReplyTo(subject, launchInputId, { timeoutMs: BACKGROUND_PROBE_TIMEOUT_MS });
  expect(launchReply.metadata?.outcome, 'the reply that launched the child must complete').toBe('completed');

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
      subject, subjectSandbox, control, controlSandbox, agentId, childRunId,
      environment: parkingEnvironment, window_seconds: IDLE_WINDOW_SECONDS,
    }),
  });
  // What the user sees before walking away: the tab strip counts the child
  // still working, and the Agents panel lists it.
  await expect(
    page.getByRole('tab', { name: 'Agents (1)', exact: true }),
    'the Agents tab must count the child the user is leaving running',
  ).toBeVisible({ timeout: 45_000 });
  await showAgentsTab(page);
  await expect(
    page.getByTestId('subagent-agents-panel').locator(`[data-child-run-id="${childRunId}"]`),
    'the Agents panel must show the child the user is leaving running',
  ).toBeVisible({ timeout: 45_000 });
  await page.goto(appPath('/sessions'), { waitUntil: 'domcontentloaded' });

  // Both conversations are left alone past the window. The control must be
  // quiet by the sweep's own reading. The subject must be as quiet as its
  // owner left it — nothing of the person's runs or waits, and nothing has
  // happened in the conversation for the window — while its child keeps
  // working; whether that work keeps the box is the runtime's status and
  // output, which is what the sweep below is asked about.
  const quietDeadline = Date.now() + QUIET_TIMEOUT_MS;
  for (;;) {
    const subjectDetail = await api.getSession(subject);
    const subjectRow = sweepRow(subject);
    const controlRow = sweepRow(control);
    const controlDetail = await api.getSession(control);
    const bothQuiet = atRestForItsOwner(subjectDetail) && (subjectRow?.activity_quiet_ms ?? -1) >= IDLE_WINDOW_MS
      && atRestForItsOwner(controlDetail) && quietForTheSweep(controlRow);
    if (bothQuiet) break;
    expect(
      parkedMark(subject),
      'the idle sweep paused the box of a conversation whose background Agent was still '
        + 'working, before either conversation had even finished its window (session='
        + `${subject} sandbox=${subjectSandbox} runtime_status=${String(subjectRow?.runtime_status)} `
        + `quiet_ms=${String(subjectRow?.quiet_ms)})`,
    ).toEqual('');
    if (Date.now() >= quietDeadline) {
      throw new Error(
        `both conversations must be left alone past their ${IDLE_WINDOW_SECONDS}s window before the `
          + `keeper can be asked about them, and they were not within ${QUIET_TIMEOUT_MS}ms. `
          + `subject: ${JSON.stringify(subjectRow)} `
          + `current_responses=${JSON.stringify(subjectDetail.current_responses ?? [])} `
          + `held_inputs=${JSON.stringify(subjectDetail.held_inputs ?? [])}; `
          + `control: ${JSON.stringify(controlRow)}. A subject that never settles means something `
          + 'of the person\'s is still running; a control that never settles means its conversation '
          + 'is doing something it was never asked to.',
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }
  /** The child's own reading and its transcript: still working, its held command owed. */
  const childStillWorking = async (): Promise<string> => {
    const row = (await api.listChildRuns(subject)).child_runs.find((item) => item.child_run_id === childRunId);
    if (!row) return 'the child run is gone from the projection';
    if (row.closed || !row.active) {
      return `the child run reads ${JSON.stringify({ closed: row.closed, active: row.active, status: row.engine_status })}`;
    }
    const calls = childBash(subject, heartbeatPath);
    if (calls.length !== 1) return `the child's transcript holds ${calls.length} heartbeat Bash calls`;
    if (calls[0]!.results.length > 0) {
      return `the child's held Bash call has a result: ${JSON.stringify(calls[0]!.results)}`;
    }
    return '';
  };
  const ticks: Array<Record<string, number>> = [];
  let controlParkedAt = parkedMark(control);
  let shortTicks = 0;
  let sawUntruncatedTick = false;
  for (let attempt = 1; attempt <= SWEEP_ATTEMPTS; attempt += 1) {
    const disqualified = disqualifiedFromTheSweep(subject);
    expect(
      disqualified,
      `the subject stopped qualifying for the idle sweep, so its survival proves nothing: `
        + `${disqualified} (session=${subject} sandbox=${subjectSandbox})`,
    ).toEqual('');
    const finished = await childStillWorking();
    expect(
      finished,
      'the background Agent finished before the keeper ran, so there was no work for the '
        + `keeper to take a box away from (session=${subject}). The child is gated on a release `
        + 'file this spec has not written yet, so a finished child here means it failed.',
    ).toEqual('');

    const tick = sweepCounters(await platform.idleSweep());
    ticks.push(tick);
    const candidates = Number(tick.idle_candidates || 0);
    // A short page resets the keyset cursor. The next short page covers the set.
    shortTicks = candidates >= 1 && candidates < IDLE_SWEEP_SCAN_LIMIT ? shortTicks + 1 : 0;
    if (shortTicks >= 2) sawUntruncatedTick = true;

    const subjectRow = sweepRow(subject);
    expect(
      String(subjectRow?.sandbox_parked_at || ''),
      `the idle sweep parked a working child: session=${subject} sandbox=${subjectSandbox} `
        + `parked_at=${String(subjectRow?.sandbox_parked_at)} runtime_status=${String(subjectRow?.runtime_status)} `
        + `(the runtime's own report the sweep reads) quiet_ms=${String(subjectRow?.quiet_ms)} `
        + `last_output_at=${String(subjectRow?.last_output_at)} child=${childRunId} tick=${JSON.stringify(tick)}`,
    ).toEqual('');

    controlParkedAt = parkedMark(control);
    if (controlParkedAt && sawUntruncatedTick) break;
    await new Promise((resolve) => setTimeout(resolve, 3_000));
  }

  await test.info().attach('idle-sweep-ticks', {
    body: JSON.stringify({
      ticks,
      subject: { session: subject, sandbox: subjectSandbox, row: sweepRow(subject) },
      control: { session: control, sandbox: controlSandbox, row: sweepRow(control) },
    }, null, 2),
    contentType: 'application/json',
  });
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
    `the sweep did not complete two consecutive short pages below ${IDLE_SWEEP_SCAN_LIMIT}; `
      + 'a partial tail alone cannot '
      + 'be said to have reached this conversation. This deployment is holding too many live '
      + `conversations for the gate to mean anything — ticks=${JSON.stringify(ticks)}`,
  ).toBe(true);
  await expect.poll(async () => String((await api.getSandbox(controlSandbox)).state || '').toUpperCase(), {
    timeout: 60_000,
    message: 'the idle control must complete its native pause',
  }).toMatch(/^(PAUSED|SUCCEED)$/);
  expect(parkedMark(subject)).toBe('');
  expect(String((await api.getSandbox(subjectSandbox)).state || '').toUpperCase()).toBe('RUNNING');
  expect(await childStillWorking()).toEqual('');

  await openSessionView(page, subject);
  await expect(
    page.getByRole('tab', { name: 'Agents (1)', exact: true }),
    'coming back must find the background Agent still counted as working',
  ).toBeVisible({ timeout: 45_000 });
  const firstBeat = await heartbeats();
  expect(
    firstBeat,
    `the background subagent wrote no heartbeat at all into ${heartbeatPath}; it never started `
      + 'its command, so nothing in this run was ever at risk from the sweep',
  ).toBeGreaterThan(0);
  await expect
    .poll(
      heartbeats,
      {
        timeout: 20_000,
        intervals: [1_500, 2_000],
        message:
          'the background subagent stopped executing: its heartbeat did not advance after the '
          + `idle sweep ran (${firstBeat} beats before, session=${subject} sandbox=${subjectSandbox}). `
          + 'An unparked database row is not the same as work that survived.',
      },
    )
    .toBeGreaterThan(firstBeat);
  await api.uploadFileText(subject, root, releaseName, 'release');
  const closed = await api.waitForChildRuns(
    subject,
    (rows) => rows.length >= 1 && rows.every((row) => row.closed),
    CHILD_SETTLED_TIMEOUT_MS,
  );
  expect(
    closed.filter((row) => row.child_run_id === childRunId).map((row) => row.engine_status),
    'the released background Agent must close its child run with Claude\'s own completed status',
  ).toEqual(['completed']);
  await expect.poll(
    () => JSON.stringify(childBash(subject, heartbeatPath).map((call) => call.results)),
    {
      timeout: CHILD_SETTLED_TIMEOUT_MS,
      message: 'the child\'s own transcript must hold its held command\'s result, printed after the release',
    },
  ).toContain(doneMarker);
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
  // Claude answers the finished child on its own: one response no input
  // started. The conversation is at rest only once that answer has ended.
  await expect
    .poll(
      () => sessionResponseRows(subject)
        .filter((row) => row.origin === 'engine')
        .map((row) => ({ status: row.status, input_ids: row.input_ids })),
      {
        timeout: CHILD_SETTLED_TIMEOUT_MS,
        intervals: [1_000, 2_000],
        message: 'Claude must answer the finished child with one response of its own that ends',
      },
    )
    .toEqual([{ status: 'ended', input_ids: [] }]);
  await expect
    .poll(
      async () => {
        const detail = await api.getSession(subject);
        return String(detail.state || '') === 'READY' && atRestForItsOwner(detail);
      },
      {
        timeout: CHILD_SETTLED_TIMEOUT_MS,
        intervals: [1_000, 2_000],
        message: 'the conversation must come to rest once the child and the runtime\'s answer to it are done',
      },
    )
    .toBe(true);
  await expect(
    page.getByRole('tab', { name: 'Agents', exact: true }),
    'the Agents tab must stop counting the child once it has closed',
  ).toBeVisible({ timeout: 45_000 });
  await expect(
    headerStatusPill(page),
    'the header must settle once the background work is done',
  ).toHaveAttribute('data-pulse', 'false', { timeout: 45_000 });
});
