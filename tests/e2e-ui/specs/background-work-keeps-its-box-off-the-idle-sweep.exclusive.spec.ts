/** A held native child survives idle reclamation while an idle control actually pauses. */
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { sessionDoc, sessionEvents, snapshotDoc } from '../fixtures/dbOracle';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { expectComposerEnabled, openSessionView } from '../fixtures/sessionPage';
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
const SWEEP_ATTEMPTS = 3;
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

function parkedMark(sessionId: string): string {
  return String(sessionDoc(sessionId)?.sandbox_parked_at || '').trim();
}

function quietForMs(sessionId: string): { state: string; quietMs: number | null } {
  const snapshot = snapshotDoc(sessionId);
  if (!snapshot) return { state: '<no snapshot>', quietMs: null };
  const updatedAt = Date.parse(String(snapshot.updated_at || ''));
  return {
    state: String(snapshot.conversation_state || ''),
    quietMs: Number.isFinite(updatedAt) ? Date.now() - updatedAt : null,
  };
}

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
    prewarm_enabled: false,
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
    'The child must use Bash to run this exact command:',
    heartbeatProgram(heartbeatPath, releasePath, doneMarker),
    `The child must report ${doneMarker} verbatim after the command completes.`,
    `Immediately after launching, reply only ${parentMarker} verbatim and end your turn.`,
    'Do not wait for the child and do not run Bash in the parent.',
  ].join('\n');
  const composer = page.getByTestId('composer-prompt');
  await composer.fill(prompt);
  const submit = page.getByTestId('composer-submit');
  await expect(submit).toBeEnabled({ timeout: 15_000 });
  await submit.click();
  const controlCreated = await api.startConversation(agentId);
  const control = String(controlCreated.session_id || '').trim();
  expect(control, 'the control conversation must open a session').not.toEqual('');
  sessions.push(control);
  await expect.poll(async () => Boolean((await api.getSession(subject)).background_task_state), {
    timeout: BACKGROUND_PROBE_TIMEOUT_MS,
    message: 'the native child must open a background manifest before the idle window',
  }).toBe(true);

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
  await expect(pill).toHaveAttribute('data-state', 'PROCESSING');
  await expect(pill).toHaveAttribute('data-pulse', 'true');
  await expect(pill).toHaveAttribute('data-tone', 'astra');
  await page.goto(appPath('/sessions'), { waitUntil: 'domcontentloaded' });
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
    // A short page resets the keyset cursor. The next short page covers the set.
    shortTicks = candidates >= 1 && candidates < IDLE_SWEEP_SCAN_LIMIT ? shortTicks + 1 : 0;
    if (shortTicks >= 2) sawUntruncatedTick = true;

    const subjectParkedAt = parkedMark(subject);
    const subjectQuiet = quietForMs(subject);
    expect(
      subjectParkedAt,
      `the idle sweep parked a working child: session=${subject} sandbox=${subjectSandbox} `
        + `parked_at=${subjectParkedAt} quiet=${subjectQuiet.quietMs}ms `
        + `open_manifests=${JSON.stringify(stillOpen)} tick=${JSON.stringify(tick)}`,
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
  expect(openBackgroundManifests(subject).length).toBeGreaterThan(0);

  await openSessionView(page, subject);
  await showAgentsTab(page);
  await expect(
    headerStatusPill(page),
    'coming back must find the background work still running',
  ).toHaveText('Running in background', { timeout: 45_000 });
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
  await api.uploadFileText(subject, root, releaseName, 'release');
  const closed = await api.waitForChildRuns(
    subject,
    (rows) => rows.length >= 1 && rows.every((row) => row.closed),
    CHILD_SETTLED_TIMEOUT_MS,
  );
  expect(
    closed.length,
    'the released background subagent must close its child run',
  ).toBeGreaterThanOrEqual(1);
  await expect.poll(() => sessionEvents(subject).some((event) => {
    if (event.event_type !== 'engine.message') return false;
    const payload = event.payload as Record<string, unknown> | undefined;
    const message = payload?.message as Record<string, unknown> | undefined;
    const data = message?.data as Record<string, unknown> | undefined;
    return message?.subtype === 'task_notification'
      && ['TaskNotificationMessage', 'SystemMessage'].includes(String(message.__sdk_type))
      && data?.status === 'completed' && Boolean(data.task_id) && JSON.stringify(data).includes(doneMarker);
  }), { timeout: CHILD_SETTLED_TIMEOUT_MS }).toBe(true);
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
