/**
 * E2E: an open background Agent survives an instance restart and rebuilds its
 * completed transcript drawer from durable engine state.
 *
 * The restart happens only after the launch turn has opened a durable
 * background manifest and before that manifest materializes. Recovery must
 * append the terminal child facts while the native parent continuation waits
 * for release, before its Result can flush them. The browser comes back cold, so a Done row and
 * a completed Bash card can only come from the mirrored child transcript, not
 * from the page's pre-restart registry.
 *
 * This is exclusive because restarting the shared server disrupts every other
 * session on that host. `run-round.mjs` detects restartServerContainer callers
 * and puts them in their own serial pass.
 */
import { expect, test, type Page } from '@playwright/test';

import {
  AstraApi,
  type ChildRunRecord,
  type MessageRecord,
  type SessionRecord,
} from '../fixtures/astraApi';
import { oracleDbPath, sessionEvents } from '../fixtures/dbOracle';
import { absoluteBaseUrl, parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import {
  requireSandboxHandle,
  restartServerContainer,
  sandboxRunning,
} from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

const OPEN_WINDOW_TIMEOUT_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_SUBAGENT_REPLAY_OPEN_TIMEOUT_MS',
  150_000,
);
const MATERIALIZATION_TIMEOUT_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_SUBAGENT_REPLAY_MATERIALIZATION_TIMEOUT_MS',
  240_000,
);
const BACKGROUND_SLEEP_SECONDS = 45;
const TERMINAL_SESSION_STATES = new Set([
  'RECOVERY_REQUIRED',
  'TERMINATED',
  'DELETED',
  'FAILED',
]);

interface OpenWindowProbe {
  window: {
    event: Record<string, unknown>;
    publicChildRunId: string;
  } | null;
  diagnostic: string;
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function nonEmptyStrings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map((item) => String(item).trim()).filter(Boolean)
    : [];
}

function stringMap(value: unknown): Record<string, string> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .map(([key, item]) => [key.trim(), String(item).trim()])
      .filter(([key, item]) => key && item),
  );
}

function expectPublicRestToHideNativeRefs(
  value: unknown,
  nativeRefs: string[],
  label: string,
): void {
  const wire = JSON.stringify(value);
  for (const key of [
    'engine_ref',
    'parent_engine_ref',
    'control_ref',
    'transcript_ref',
    'engineRef',
    'parentEngineRef',
    'controlRef',
    'transcriptRef',
    'engine_refs',
    'control_to_engine_ref',
    'transcript_refs',
    'transcript_to_engine_ref',
  ]) {
    expect(wire, `${label} must not expose private field ${key}`).not.toContain(`"${key}"`);
  }
  for (const nativeRef of nativeRefs.filter(Boolean)) {
    expect(wire, `${label} must not expose native reference ${nativeRef}`).not.toContain(nativeRef);
  }
}

function lifecycleFacts(event: Record<string, unknown>): Record<string, unknown>[] {
  const blocks = eventPayload(event).blocks;
  if (!Array.isArray(blocks)) return [];
  return blocks.flatMap((block) => {
    if (!block || typeof block !== 'object') return [];
    const data = (block as Record<string, unknown>).data;
    if (!data || typeof data !== 'object' || Array.isArray(data)) return [];
    const fact = data as Record<string, unknown>;
    return fact.kind === 'lifecycle' ? [fact] : [];
  });
}

function eventPayload(event: Record<string, unknown>): Record<string, unknown> {
  return event.payload && typeof event.payload === 'object'
    ? (event.payload as Record<string, unknown>)
    : {};
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

async function waitForOpenBackgroundWindow(
  api: AstraApi,
  sessionId: string,
  timeoutMs: number,
): Promise<OpenWindowProbe> {
  const deadline = Date.now() + timeoutMs;
  let lastSession: SessionRecord | null = null;
  let lastChildRuns: ChildRunRecord[] = [];
  let lastEvents: Record<string, unknown>[] = [];

  while (Date.now() < deadline) {
    const [session, childRunPage] = await Promise.all([
      api.getSession(sessionId),
      api.listChildRuns(sessionId),
    ]);
    lastSession = session;
    lastChildRuns = childRunPage.child_runs;
    lastEvents = sessionEvents(sessionId);

    if (TERMINAL_SESSION_STATES.has(String(session.state || ''))) {
      throw new Error(
        `session ${sessionId} reached ${session.state} while waiting for a background replay window; `
        + `last_error=${session.last_error || '<none>'}`,
      );
    }

    const opened = lastEvents.find(
      (event) => String(event.event_type || '') === 'turn.background_tasks_opened',
    );
    if (opened) {
      const openedEventSeq = Number(opened.event_seq);
      const backgroundState = session.background_task_state;
      const stateOpenedEventSeq = backgroundState && typeof backgroundState === 'object'
        ? Number((backgroundState as Record<string, unknown>).opened_event_seq)
        : Number.NaN;
      const materialized = materializationsForOpenedEvent(lastEvents, openedEventSeq);
      if (
        Number.isFinite(openedEventSeq)
        && openedEventSeq > 0
        && stateOpenedEventSeq === openedEventSeq
        && materialized.length === 0
        && lastChildRuns.length === 1
      ) {
        return {
          window: {
            event: opened,
            publicChildRunId: lastChildRuns[0].child_run_id,
          },
          diagnostic: '',
        };
      }
      if (materialized.length > 0) {
        return {
          window: null,
          diagnostic:
            `the Agent settled before the OPEN window could be faulted `
            + `(opened_event_seq=${openedEventSeq}, materialized=${materialized.length})`,
        };
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 1_500));
  }

  const eventTypes = lastEvents.map((event) => String(event.event_type || '')).filter(Boolean);
  return {
    window: null,
    diagnostic:
      `no OPEN turn.background_tasks_opened manifest appeared within ${timeoutMs}ms `
      + `(session_state=${lastSession?.state || '<none>'}, current_turn_id=${lastSession?.current_turn_id || '<none>'}, `
      + `child_runs=${JSON.stringify(lastChildRuns)}, `
      + `journal_event_types=${JSON.stringify(eventTypes)})`,
  };
}

async function waitForRecoveredMaterialization(
  api: AstraApi,
  sessionId: string,
  openedEventSeq: number,
  timeoutMs: number,
): Promise<{
  session: SessionRecord;
  messages: MessageRecord[];
  childRuns: ChildRunRecord[];
  events: Record<string, unknown>[];
}> {
  const deadline = Date.now() + timeoutMs;
  let lastSession: SessionRecord | null = null;
  let lastMessages: MessageRecord[] = [];
  let lastChildRuns: ChildRunRecord[] = [];
  let lastEvents: Record<string, unknown>[] = [];

  while (Date.now() < deadline) {
    const [session, messagePage, childRunPage] = await Promise.all([
      api.getSession(sessionId),
      api.getMessages(sessionId, 50),
      api.listChildRuns(sessionId),
    ]);
    lastSession = session;
    lastMessages = messagePage.messages || [];
    lastChildRuns = childRunPage.child_runs;
    lastEvents = sessionEvents(sessionId);
    if (TERMINAL_SESSION_STATES.has(String(session.state || ''))) {
      throw new Error(
        `session ${sessionId} reached ${session.state} during background recovery; `
        + `last_error=${session.last_error || '<none>'}`,
      );
    }
    if (
      materializationsForOpenedEvent(lastEvents, openedEventSeq).length > 0
      && !session.background_task_state
      && lastChildRuns.some((childRun) => childRun.closed)
    ) {
      return { session, messages: lastMessages, childRuns: lastChildRuns, events: lastEvents };
    }
    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }

  throw new Error(
    `background manifest ${openedEventSeq} did not materialize after instance recovery `
    + `within ${timeoutMs}ms (session_state=${lastSession?.state || '<none>'}, `
    + `background_task_state=${JSON.stringify(lastSession?.background_task_state || null)}, `
    + `child_runs=${JSON.stringify(lastChildRuns)}, `
    + `journal_event_types=${JSON.stringify(lastEvents.map((event) => event.event_type))})`,
  );
}

function backgroundAgentPrompt(
  runId: number,
  marker: string,
  startedPath: string,
  releasePath: string,
): string {
  return [
    `E2E subagent replay recovery ${runId}. Follow every instruction exactly.`,
    'Launch exactly one Agent tool call with run_in_background=true and subagent_type=general-purpose.',
    'That background Agent must call Bash exactly once with this exact command:',
    `sleep ${BACKGROUND_SLEEP_SECONDS} && printf '${marker}\\n'`,
    `After Bash succeeds, its final answer must be exactly ${marker}_DONE.`,
    'Before that background completion notification arrives, the parent must not call Bash, TaskOutput, or wait.',
    `As soon as the Agent tool returns its background task id, reply exactly PARENT_LAUNCHED_${runId}.`,
    'Later, when the background completion notification starts your automatic continuation, do not reply yet.',
    'The parent must call Bash exactly once with run_in_background=false and timeout=120000, running this complete command:',
    "python3 - <<'PY'",
    'from pathlib import Path',
    'import time',
    `Path(${JSON.stringify(startedPath)}).write_text('started')`,
    `release = Path(${JSON.stringify(releasePath)})`,
    'deadline = time.monotonic() + 90',
    'while not release.exists():',
    '    if time.monotonic() >= deadline:',
    "        raise TimeoutError('recovery test did not release the parent')",
    '    time.sleep(0.1)',
    `print('PARENT_RELEASED_${runId}')`,
    'PY',
    'Wait for this Bash call to finish before giving your final reply. Do not create the release file yourself.',
  ].join('\n');
}

async function showAgentsTab(page: Page): Promise<void> {
  await page.getByRole('tab', { name: /^Agents/ }).click();
  await expect(page.getByTestId('subagent-agents-panel')).toBeVisible({ timeout: 15_000 });
}

let sessionId = '';
onPassOnly(async ({ request }) => {
  if (sessionId) await new AstraApi(request).interruptSession(sessionId);
});
const sessions = trackSessions();

test('background Agent recovery replays its durable transcript drawer', async ({ page, request }) => {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const runId = Date.now();
  const childMarker = `REPLAYED_SUBAGENT_${runId}`;
  const startedPath = `/workspace/.astrabox-e2e-replay-${runId}.started`;
  const releasePath = `/workspace/.astrabox-e2e-replay-${runId}.release`;

  // The open/materialized journal pair is the durable boundary under test.
  // Resolve the oracle before creating anything so a missing deployment handle
  // skips with setup instructions and leaves no session behind.
  test.info().annotations.push({ type: 'oracle-db', description: oracleDbPath() });

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  sessionId = created.session_id;
  sessions.push(sessionId);

  const ready = await api.waitForSessionReady(sessionId);
  await api.setPermissionMode(sessionId, 'bypassPermissions');
  const sandboxId = String(ready.sandbox_id || '').trim();
  expect(sandboxId, 'the replay scenario requires the session runtime published by the platform').not.toBe('');

  await page.setViewportSize({ width: 1280, height: 720 });
  await openSessionView(page, sessionId);
  const prompt = backgroundAgentPrompt(runId, childMarker, startedPath, releasePath);
  await sendPrompt(page, sessionId, prompt);
  await expect(
    page.getByTestId('user-message').last(),
    'the browser must show the launch instruction it dispatched',
  ).toContainText(childMarker, { timeout: 30_000 });

  // Background work opens from the engine's `async_launched` receipt at the
  // launching Result. Keeping the parent turn open would prevent this manifest
  // from existing and exercise a different contract.
  const probe = await waitForOpenBackgroundWindow(
    api,
    sessionId,
    OPEN_WINDOW_TIMEOUT_MS,
  );
  expect(probe.window, probe.diagnostic).not.toBeNull();

  const openedEvent = probe.window!.event;
  const openedEventSeq = Number(openedEvent.event_seq);
  const openedPayload = eventPayload(openedEvent);
  const transcriptRefs = nonEmptyStrings(openedPayload.transcript_refs);
  const engineRefs = nonEmptyStrings(openedPayload.engine_refs);
  const transcriptToEngineRef = stringMap(openedPayload.transcript_to_engine_ref);
  const controlToEngineRef = stringMap(openedPayload.control_to_engine_ref);
  const controlRefs = Object.keys(controlToEngineRef);
  expect(transcriptRefs, 'one async launch must name its private transcript').toHaveLength(1);
  expect(engineRefs, 'one async launch must name its private engine child').toHaveLength(1);
  expect(
    transcriptToEngineRef,
    'the private manifest must bind the transcript locator to the logical engine child',
  ).toEqual({ [transcriptRefs[0]]: engineRefs[0] });
  // TaskStarted may precede the launching Result. In that ordering Claude has
  // already published its task_id control locator; otherwise ordered replay
  // enriches the same child after the open manifest is persisted.
  expect(
    Object.values(controlToEngineRef).every((engineRef) => engineRef === engineRefs[0]),
    'every lifecycle-published stop handle must resolve to the launched engine child',
  ).toBe(true);
  expect(
    openedPayload,
    'the private manifest must not preserve the old public-id list',
  ).not.toHaveProperty('child_run_ids');
  expect(
    openedPayload,
    'the private manifest must not preserve the old control-id list',
  ).not.toHaveProperty('control_ids');

  const childRunId = probe.window!.publicChildRunId;
  expect(childRunId, 'the Session projection must mint one public child UUID').toMatch(UUID_PATTERN);
  expect(childRunId).not.toBe(engineRefs[0]);
  expect(childRunId).not.toBe(transcriptRefs[0]);
  const beforeRestartChildRuns = await api.listChildRuns(sessionId);
  expect(beforeRestartChildRuns.child_runs.map((childRun) => childRun.child_run_id)).toEqual([childRunId]);
  expectPublicRestToHideNativeRefs(
    beforeRestartChildRuns,
    [...engineRefs, ...transcriptRefs, ...controlRefs],
    'pre-restart child-runs REST',
  );
  const parentTurnId = String(openedEvent.turn_id || '').trim();
  expect(parentTurnId, 'the open manifest must stay bound to its launcher turn').not.toBe('');
  expect(
    materializationsForOpenedEvent(sessionEvents(sessionId), openedEventSeq),
    'the fault must land before the background completion materializes',
  ).toHaveLength(0);

  // Resolve compute through the platform-published id and endpoint. This is
  // deliberately runtime-neutral: Docker and Kubernetes are substrate choices,
  // while the invariant is that the same sandbox keeps running across an
  // instance replacement.
  const sandboxHandle = await requireSandboxHandle(api, sandboxId);
  expect(sandboxRunning(sandboxHandle), 'the background Agent sandbox must be live before restart').toBe(true);

  // Drop both live readers. The runner and its fsync spool remain inside the
  // sandbox; the new server generation and the new page must recover from them.
  await page.goto('about:blank', { waitUntil: 'domcontentloaded' });
  await restartServerContainer(absoluteBaseUrl());

  const afterRestart = await api.getSession(sessionId);
  expect(
    String(afterRestart.sandbox_id || '').trim(),
    'instance recovery must reattach the original sandbox instead of replacing it',
  ).toBe(sandboxId);
  expect(sandboxRunning(sandboxHandle), 'the original sandbox must still be running after restart').toBe(true);

  // Restore the original held-continuation scenario, using the shared file
  // channel rather than a fixed sleep or a sibling isolation's private /tmp.
  await expect.poll(async () => {
    const files = await platform.listFiles(sessionId, '/workspace', 10_000);
    if (!files.entries?.some((entry) => entry.name === startedPath.split('/').at(-1) && entry.kind === 'file')) return false;
    return await api.downloadFileText(sessionId, startedPath, 10_000) === 'started';
  }, { timeout: 60_000, intervals: [500, 1_000], message: 'the native parent continuation must enter its real Bash gate' }).toBe(true);

  const settled = await waitForRecoveredMaterialization(
    api,
    sessionId,
    openedEventSeq,
    MATERIALIZATION_TIMEOUT_MS,
  );
  const matchingMaterializations = materializationsForOpenedEvent(
    settled.events,
    openedEventSeq,
  );
  expect(
    matchingMaterializations,
    'recovery must claim one materialization for the open manifest, not replay it twice',
  ).toHaveLength(1);
  expect(
    eventPayload(matchingMaterializations[0]).source,
    'recovery must materialize from durable detached-child evidence',
  ).toBe('engine_detached_child');
  expect(
    settled.events.filter((event) => (
      Number(event.event_seq) > openedEventSeq
      && String(event.event_type || '') === 'turn.completed'
    )),
    'no later parent Result may be available to flush the recovered child facts',
  ).toHaveLength(0);
  const privateLifecycleFacts = lifecycleFacts(matchingMaterializations[0]);
  expect(privateLifecycleFacts, 'materialization must contain one private engine lifecycle fact').toHaveLength(1);
  expect(privateLifecycleFacts[0]).toEqual(expect.objectContaining({
    kind: 'lifecycle',
    engineRef: engineRefs[0],
    event: 'closed',
    engineEvent: 'task_notification',
    engineStatus: 'completed',
    operations: [],
  }));
  const controlRef = String(privateLifecycleFacts[0].controlRef || '').trim();
  expect(controlRef, 'Claude terminal evidence must retain its private control locator').not.toBe('');
  if (controlRefs.length > 0) {
    expect(
      controlRefs,
      'a stop handle observed before restart must remain the terminal lifecycle control locator',
    ).toContain(controlRef);
  }
  expect(
    settled.session.background_task_state,
    'the recovered completion must close the durable background window',
  ).toBeFalsy();

  const launcher = settled.messages.find((message) => (
    message.role === 'assistant'
    && String(message.turn_id || '').trim() === parentTurnId
  ));
  expect(launcher, 'materialization must update the original launcher message').toBeTruthy();
  const matchingChildRuns = settled.childRuns.filter(
    (item) => item.child_run_id === childRunId,
  );
  expect(
    matchingChildRuns,
    'recovery must preserve the public UUID and Claude\'s exact completed status',
  ).toEqual([
    expect.objectContaining({
      child_run_id: childRunId,
      engine_kind: 'claude_code',
      engine_status: 'completed',
      closed: true,
      operations: [],
    }),
  ]);
  expectPublicRestToHideNativeRefs(
    { session_id: sessionId, child_runs: settled.childRuns },
    [...engineRefs, ...transcriptRefs, controlRef],
    'recovered child-runs REST',
  );
  const recoveredTranscript = await api.getChildRunMessages(sessionId, childRunId);
  expect(recoveredTranscript.child_run_id, 'transcript lookup must use the same stable public UUID').toBe(childRunId);
  expectPublicRestToHideNativeRefs(
    recoveredTranscript,
    [...engineRefs, ...transcriptRefs, controlRef],
    'recovered child transcript REST',
  );
  expect(
    settled.messages.flatMap((message) => message.blocks || [])
      .filter((block) => String(block.type || '') === 'subagent'),
    'root history must not own the replayed child transcript',
  ).toHaveLength(0);

  // A cold navigation exercises the Session child-run API -> registry. A
  // lifecycle summary can make a row look complete, so the load-bearing assertion
  // is the completed Bash card and marker inside that card: those require the
  // named subagent transcript to have been replayed from the engine mirror.
  await openSessionView(page, sessionId);
  await expect(
    page.getByTestId('user-message').filter({ hasText: childMarker }),
    'the recovered conversation must retain the launch instruction',
  ).toHaveCount(1, { timeout: 45_000 });
  await showAgentsTab(page);

  const panel = page.getByTestId('subagent-agents-panel');
  const row = panel
    .getByTestId('subagent-agent-row')
    .filter({ hasText: childRunId.slice(0, 8) });
  await expect(
    row,
    'the replayed launcher must rebuild exactly one row keyed by its Agent tool call',
  ).toHaveCount(1, { timeout: 45_000 });
  await expect(
    row.getByText(/^completed$/),
    'the replayed Agent row must show Claude\'s exact completed status',
  ).toBeVisible({ timeout: 45_000 });
  await row.click();

  const drawer = page.getByTestId('subagent-transcript-drawer');
  await expect(drawer).toBeVisible({ timeout: 15_000 });
  const completedBash = drawer.getByRole('button', {
    name: /^Bash\s+(Done|完成)$/,
  }).first();
  await expect(
    completedBash,
    'the cold drawer must contain the child transcript Bash round-trip, not only its notification summary',
  ).toBeVisible({ timeout: 45_000 });
  await completedBash.click();
  await expect(
    completedBash.locator('xpath=..'),
    'the replayed Bash card must belong to this child run',
  ).toContainText(childMarker, { timeout: 15_000 });

  expect(
    sessionEvents(sessionId).filter((event) => Number(event.event_seq) > openedEventSeq
      && String(event.event_type || '') === 'turn.completed'),
    'the cold child drawer must recover before releasing the parent Result',
  ).toHaveLength(0);
  await api.uploadFileText(sessionId, '/workspace', releasePath.split('/').at(-1)!, 'release', 10_000);
  expect(await api.downloadFileText(sessionId, releasePath, 10_000)).toBe('release');
  expect((await api.waitForSessionReady(sessionId)).state, 'the released native continuation must settle normally').toBe('READY');

  console.log('SUBAGENT_REPLAY_RECOVERY_E2E_EVIDENCE', JSON.stringify({
    session_id: sessionId,
    sandbox_id: sandboxId,
    sandbox_runtime: sandboxHandle.runtime,
    parent_turn_id: parentTurnId,
    child_run_id: childRunId,
    opened_event_seq: openedEventSeq,
    materialized_event_seq: matchingMaterializations[0]?.event_seq,
    later_parent_turn_completions: 0,
  }));
});
