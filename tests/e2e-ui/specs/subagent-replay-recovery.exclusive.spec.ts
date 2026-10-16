/**
 * E2E: an open background Agent survives an instance restart and rebuilds its
 * completed transcript drawer from durable engine state.
 *
 * The restart happens after the reply that launched the Agent has ended and
 * while the child is still working. The child finishes across the restart;
 * Claude answers its completion on its own and, as the prompt asks, runs one
 * Bash command this spec holds open. The restarted server's reader takes the
 * session's lease and resumes the runtime's events from the position the old
 * process committed (`session_runtime/host.py`). Inside that held continuation
 * the browser comes back cold, so a closed `completed` row and a completed
 * Bash card in the child's drawer can only come from what the platform
 * recorded after the restart and from the child's own transcript in the
 * SessionStore mirror (`stored_child_transcript_facts` in
 * `engine/claude_code.py`) — not from the page's pre-restart registry, and not
 * from the parent's continuation ending.
 *
 * The page leaves before the restart, so nothing reads the conversation in a
 * browser between the restart and the cold drawer.
 *
 * This is exclusive because restarting the shared server disrupts every other
 * session on that host. `run-round.mjs` detects restartServerContainer callers
 * and puts them in their own serial pass.
 */
import { expect, test, type Page } from '@playwright/test';
import type { Response } from '@playwright/test';

import { AstraApi, messageToolCalls, type ChildRunRecord } from '../fixtures/astraApi';
import { nativeTranscriptRows, oracleDbPath, sessionResponseRows } from '../fixtures/dbOracle';
import { absoluteBaseUrl, parseTimeoutEnv } from '../fixtures/env';
import { PlatformApi } from '../fixtures/platformApi';
import {
  requireSandboxHandle,
  restartServerContainer,
  sandboxRunning,
} from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

test.use({ locale: 'en-US' });

const OPEN_WINDOW_TIMEOUT_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_SUBAGENT_REPLAY_OPEN_TIMEOUT_MS',
  150_000,
);
const RECOVERY_TIMEOUT_MS = parseTimeoutEnv(
  'ASTRABOX_E2E_SUBAGENT_REPLAY_MATERIALIZATION_TIMEOUT_MS',
  240_000,
);
const BACKGROUND_SLEEP_SECONDS = 45;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
// Claude Code keeps each Agent's conversation in a transcript of its own,
// `subagents/agent-<id>` in the session's SessionStore mirror
// (`engine/claude_code_background.py`).
const AGENT_TRANSCRIPT_PREFIX = 'subagents/agent-';

/** The engine's own handles on a child; the public API addresses it by `child_run_id` only. */
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
  ]) {
    expect(wire, `${label} must not expose private field ${key}`).not.toContain(`"${key}"`);
  }
  for (const nativeRef of nativeRefs.filter(Boolean)) {
    expect(wire, `${label} must not expose native reference ${nativeRef}`).not.toContain(nativeRef);
  }
}

/** The input id the inputs route answered a composer send with. */
async function acceptedInputId(response: Response): Promise<string> {
  const body = (await response.json()) as { data?: { input_id?: unknown } };
  const inputId = String(body.data?.input_id ?? '').trim();
  expect(inputId, 'the inputs route must answer with the accepted input id').not.toBe('');
  return inputId;
}

// ── Claude's own transcript, as the SessionStore mirror holds it ───────────

interface NativeEntry {
  subpath: string | null;
  entry: Record<string, unknown>;
}

function nativeEntries(sessionId: string): NativeEntry[] {
  return nativeTranscriptRows(sessionId).map((row) => ({
    subpath: row.subpath,
    entry: JSON.parse(String(row.entry_json)) as Record<string, unknown>,
  }));
}

/** A line of the parent's own lane, not a sub-agent's line carried in its file. */
function isParentLane(item: NativeEntry): boolean {
  if (item.subpath !== null || item.entry.isSidechain === true) return false;
  return String(item.entry.parentToolUseId ?? item.entry.parent_tool_use_id ?? '').trim() === '';
}

function contentBlocks(entry: Record<string, unknown>): Record<string, unknown>[] {
  const message = entry.message;
  if (!message || typeof message !== 'object') return [];
  const content = (message as Record<string, unknown>).content;
  return Array.isArray(content)
    ? content.filter((block): block is Record<string, unknown> => Boolean(block) && typeof block === 'object')
    : [];
}

/** Claude's launch receipt for this run's backgrounded Agent call: the Agent's own id. */
function nativeAgentIds(sessionId: string, marker: string): string[] {
  const parent = nativeEntries(sessionId).filter(isParentLane);
  const calls = parent.flatMap((item) => contentBlocks(item.entry))
    .filter((block) => block.type === 'tool_use' && ['Agent', 'Task'].includes(String(block.name))
      && (block.input as Record<string, unknown> | undefined)?.run_in_background === true
      && JSON.stringify(block.input).includes(marker));
  return calls.flatMap((call) => parent.flatMap((item) => {
    const result = item.entry.toolUseResult as Record<string, unknown> | undefined;
    const receipt = result?.status === 'async_launched'
      && contentBlocks(item.entry).some((block) => block.type === 'tool_result' && block.tool_use_id === call.id);
    return receipt && result?.agentId ? [String(result.agentId)] : [];
  }));
}

interface NativeBash {
  toolUseId: string;
  input: Record<string, unknown>;
  results: Record<string, unknown>[];
}

/** Bash calls whose input carries `fragment`, with their results, in one transcript lane. */
function nativeBash(entries: NativeEntry[], fragment: string): NativeBash[] {
  const blocks = entries.flatMap((item) => contentBlocks(item.entry));
  const calls = new Map<string, Record<string, unknown>>();
  for (const block of blocks) {
    if (block.type !== 'tool_use' || block.name !== 'Bash') continue;
    if (!JSON.stringify(block.input ?? null).includes(fragment)) continue;
    calls.set(String(block.id ?? ''), (block.input ?? {}) as Record<string, unknown>);
  }
  return [...calls].filter(([toolUseId]) => toolUseId).map(([toolUseId, input]) => ({
    toolUseId,
    input,
    results: blocks.filter((block) => block.type === 'tool_result' && block.tool_use_id === toolUseId),
  }));
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

  // The session's records are the oracle across the restart. Resolve the
  // database before creating anything so a missing deployment handle fails
  // with setup instructions and leaves no session behind.
  test.info().annotations.push({ type: 'oracle-db', description: oracleDbPath() });

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  sessionId = created.session_id;
  sessions.push(sessionId);

  const ready = await api.waitForSessionReady(sessionId);
  await api.setPermissionMode(sessionId, 'bypassPermissions');
  const sandboxId = String(ready.sandbox_id || '').trim();
  expect(sandboxId, 'the replay scenario requires the session runtime published by the platform').not.toBe('');
  const root = String((await platform.listFiles(sessionId)).root_path || '').trim();
  expect(root, 'the files listing must name the workspace root the gate lives in').not.toBe('');
  const startedPath = `${root}/.astrabox-e2e-replay-${runId}.started`;
  const releasePath = `${root}/.astrabox-e2e-replay-${runId}.release`;

  await page.setViewportSize({ width: 1280, height: 720 });
  await openSessionView(page, sessionId);
  const prompt = backgroundAgentPrompt(runId, childMarker, startedPath, releasePath);
  const launchInputId = await acceptedInputId(await sendPrompt(page, sessionId, prompt));
  await expect(
    page.getByTestId('user-message').last(),
    'the browser must show the launch instruction it dispatched',
  ).toContainText(childMarker, { timeout: 30_000 });

  // ── The window: the launch reply has ended and its one child still works.
  const launchReply = await api.waitForReplyTo(sessionId, launchInputId, { timeoutMs: OPEN_WINDOW_TIMEOUT_MS });
  expect(launchReply.metadata?.outcome, 'the reply that launched the Agent must complete').toBe('completed');
  const open = await api.waitForChildRuns(sessionId, (rows) => rows.length === 1, OPEN_WINDOW_TIMEOUT_MS);
  expect(
    open[0]!.closed,
    'the Agent settled before the restart could land inside its work; the scene was not reached',
  ).toBe(false);
  const childRunId = open[0]!.child_run_id;
  await expect.poll(() => nativeAgentIds(sessionId, childMarker), {
    timeout: 15_000,
    message: 'Claude\'s own transcript must hold one backgrounded Agent call with its async_launched receipt',
  }).toHaveLength(1);
  const agentIdNative = nativeAgentIds(sessionId, childMarker)[0]!;
  expect(childRunId, 'the Session projection must mint one public child UUID').toMatch(UUID_PATTERN);
  expect(childRunId).not.toBe(agentIdNative);
  expectPublicRestToHideNativeRefs(
    await api.listChildRuns(sessionId),
    [agentIdNative],
    'pre-restart child-runs REST',
  );
  expect(
    sessionResponseRows(sessionId).filter((row) => row.origin === 'engine'),
    'the restart must land before Claude answers the finished child',
  ).toEqual([]);

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

  // Claude's own continuation enters its real Bash gate: the child finished and
  // Claude is answering it, through the shared file channel rather than a fixed
  // sleep or a sibling isolation's private /tmp.
  await expect.poll(async () => {
    const files = await platform.listFiles(sessionId, root, 10_000);
    if (!files.entries?.some((entry) => entry.name === startedPath.split('/').at(-1) && entry.kind === 'file')) return false;
    return await api.downloadFileText(sessionId, startedPath, 10_000) === 'started';
  }, { timeout: 60_000, intervals: [500, 1_000], message: 'the native parent continuation must enter its real Bash gate' }).toBe(true);

  // ── What the restarted platform recorded, while the continuation is held.
  let recovered: ChildRunRecord[] = [];
  await expect.poll(async () => {
    recovered = (await api.listChildRuns(sessionId)).child_runs.filter((row) => row.child_run_id === childRunId);
    return {
      child: recovered.map((row) => ({
        engine_kind: row.engine_kind, engine_status: row.engine_status, closed: row.closed, operations: row.operations,
      })),
      answers: sessionResponseRows(sessionId).filter((row) => row.origin === 'engine')
        .map((row) => ({ status: row.status, input_ids: row.input_ids })),
    };
  }, {
    timeout: RECOVERY_TIMEOUT_MS,
    intervals: [1_000, 2_000],
    message: 'after the restart, the child must read closed at Claude\'s exact completed status, under the '
      + 'same public UUID, while Claude\'s own answer to it is the one response still running',
  }).toEqual({
    child: [{ engine_kind: 'claude_code', engine_status: 'completed', closed: true, operations: [] }],
    answers: [{ status: 'running', input_ids: [] }],
  });
  const heldParent = () => nativeBash(nativeEntries(sessionId).filter(isParentLane), startedPath);
  await expect.poll(() => heldParent().length, {
    timeout: 15_000,
    message: 'Claude\'s transcript must hold the parent continuation\'s one gated Bash call',
  }).toBe(1);
  expect(
    heldParent()[0]!.input.run_in_background,
    'the gated command must run in the parent\'s foreground, so its result stays owed until release',
  ).not.toBe(true);
  expect(
    heldParent()[0]!.results,
    'the parent continuation must still be held: no result for its Bash call, so nothing after it can carry the child',
  ).toEqual([]);
  expectPublicRestToHideNativeRefs(
    { session_id: sessionId, child_runs: recovered },
    [agentIdNative],
    'recovered child-runs REST',
  );

  // The child's transcript, through its public id, against the child's own
  // record of the same Bash round trip.
  const childNative = () => nativeEntries(sessionId)
    .filter((item) => item.subpath === `${AGENT_TRANSCRIPT_PREFIX}${agentIdNative}`);
  await expect.poll(() => nativeBash(childNative(), childMarker).map((call) => call.results.length), {
    timeout: 15_000,
    message: 'the child\'s own transcript must hold its one Bash call and that call\'s result',
  }).toEqual([1]);
  const childBash = nativeBash(childNative(), childMarker);
  expect(
    JSON.stringify(childBash[0]!.results),
    'the child\'s own transcript must hold that call\'s result with the printed marker',
  ).toContain(childMarker);
  const recoveredTranscript = await api.getChildRunMessages(sessionId, childRunId);
  expect(recoveredTranscript.child_run_id, 'transcript lookup must use the same stable public UUID').toBe(childRunId);
  expectPublicRestToHideNativeRefs(recoveredTranscript, [agentIdNative], 'recovered child transcript REST');
  const childBlocks = recoveredTranscript.messages.flatMap((message) => message.content);
  expect(
    childBlocks.filter((block) => block.type === 'tool_use' && block.id === childBash[0]!.toolUseId)
      .map((block) => block.name),
    'the child transcript must carry the child\'s own Bash call once',
  ).toEqual(['Bash']);
  expect(
    JSON.stringify(childBlocks.filter((block) => block.type === 'tool_result'
      && block.tool_use_id === childBash[0]!.toolUseId)),
    'the child transcript must carry that call\'s result',
  ).toContain(childMarker);

  const history = await api.getMessages(sessionId, 50);
  expect(
    history.messages.flatMap((message) => message.parts).filter((part) => part.type === 'data-subagent'),
    'root history must not own the child\'s lifecycle or transcript',
  ).toEqual([]);
  expect(
    history.messages.flatMap(messageToolCalls).filter((call) => call.toolCallId === childBash[0]!.toolUseId),
    'root history must not own the child\'s tool calls',
  ).toEqual([]);

  // A cold navigation exercises the Session child-run API -> registry. A
  // lifecycle summary can make a row look complete, so the load-bearing assertion
  // is the completed Bash card and marker inside that card: those require the
  // child's transcript to have been read from the engine mirror.
  await openSessionView(page, sessionId);
  await expect(
    page.getByTestId('user-message').filter({ hasText: childMarker }),
    'the recovered conversation must retain the launch instruction',
  ).toHaveCount(1, { timeout: 45_000 });
  await showAgentsTab(page);

  const panel = page.getByTestId('subagent-agents-panel');
  const row = panel.locator(`[data-child-run-id="${childRunId}"]`);
  await expect(
    row,
    'the recovered Agent must render exactly one row under its public UUID',
  ).toHaveCount(1, { timeout: 45_000 });
  await expect(
    row.getByText(/^completed$/),
    'the recovered Agent row must show Claude\'s exact completed status',
  ).toBeVisible({ timeout: 45_000 });
  await row.click();

  const drawer = page.getByTestId('subagent-transcript-drawer');
  await expect(drawer).toBeVisible({ timeout: 15_000 });
  const completedBash = drawer.getByRole('button', {
    name: /^Bash\s+(Done|已完成)$/,
  }).first();
  await expect(
    completedBash,
    'the cold drawer must contain the child transcript Bash round-trip, not only its notification summary',
  ).toBeVisible({ timeout: 45_000 });
  await completedBash.click();
  await expect(
    completedBash.locator('xpath=..'),
    'the recovered Bash card must belong to this child run',
  ).toContainText(childMarker, { timeout: 15_000 });

  expect(
    sessionResponseRows(sessionId).filter((row) => row.origin === 'engine').map((row) => row.status),
    'the cold child drawer must recover before the parent continuation ends',
  ).toEqual(['running']);
  await api.uploadFileText(sessionId, root, releasePath.split('/').at(-1)!, 'release', 10_000);
  expect(await api.downloadFileText(sessionId, releasePath, 10_000)).toBe('release');
  expect((await api.waitForSessionReady(sessionId)).state, 'the released native continuation must settle normally').toBe('READY');
  expect(
    sessionResponseRows(sessionId).filter((row) => row.origin === 'engine')
      .map((row) => ({ status: row.status, outcome: row.outcome })),
    'Claude\'s own continuation must end completed once released',
  ).toEqual([{ status: 'ended', outcome: 'completed' }]);

  console.log('SUBAGENT_REPLAY_RECOVERY_E2E_EVIDENCE', JSON.stringify({
    session_id: sessionId,
    sandbox_id: sandboxId,
    sandbox_runtime: sandboxHandle.runtime,
    launch_response_id: launchReply.id,
    child_run_id: childRunId,
  }));
});
