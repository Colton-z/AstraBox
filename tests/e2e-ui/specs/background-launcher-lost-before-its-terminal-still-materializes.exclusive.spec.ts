/**
 * E2E: a background Agent survives its conversation losing the platform's
 * connection to the runtime before the reply that launched it ends.
 *
 * Hold the session's reader at the launch receipt (the CLI's `async_launched`
 * tool result), evict only the host's runtime connection, and release the
 * reader while the child continues in the same live sandbox. The reader
 * reconnects to the same runtime from the position it committed
 * (`session_runtime/host.py`); the user does nothing.
 *
 * Required, with nobody sending anything else: the launch reply completes once,
 * as Claude wrote it; the platform tracks exactly one child, which finishes its
 * work in the live box and closes with Claude's own completed status; Claude's
 * own answer to the finished child reaches the conversation as one response of
 * origin `engine` that no input started; and a cold open shows that answer, the
 * child in the Agents panel at its exact status, and a settled header.
 *
 * Claude's run_in_background and async_launched vocabulary arrange the scene.
 * A per-session reader hold proves the connection was lost before the launch
 * reply's end. Failed sessions retain their sandboxes for diagnosis; the test
 * does not restart shared services or destroy another conversation's compute.
 */
import { expect, test } from '@playwright/test';
import type { Response } from '@playwright/test';

import { AstraApi, messageText, type ChildRunRecord } from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import {
  nativeTranscriptRows,
  oracleDbPath,
  sessionInputRows,
  sessionResponseRows,
} from '../fixtures/dbOracle';
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
import { normalizeRendered } from '../fixtures/renderedTranscript';
import { requireSandboxHandle, sandboxRunning } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';

// Per-step ceilings, not expectations. The lane's wall is the runner's hard cut
// (suite-contract max_test_seconds), and a spec never states its own budget —
// these only decide which named assertion reports a stall.
const READY_MS = parseTimeoutEnv('ASTRABOX_E2E_BG_LOST_READY_MS', 90_000);
// Reaching the launch: the hold consuming this session's declaration AND the
// child's own first write landing in the workspace.
const LAUNCH_MS = parseTimeoutEnv('ASTRABOX_E2E_BG_LOST_LAUNCH_MS', 90_000);
// The launch reply ending after the eviction and the release.
const SETTLE_MS = parseTimeoutEnv('ASTRABOX_E2E_BG_LOST_SETTLE_MS', 90_000);
// How long the child's completion, and Claude's answer to it, may take to reach
// the conversation.
const COMPLETION_MS = parseTimeoutEnv('ASTRABOX_E2E_BG_LOST_MATERIALIZE_MS', 120_000);

// Long enough that the launch reply is certainly cut off while the child is
// still working, short enough to leave the completion room inside the wall.
const CHILD_SLEEP_SECONDS = Number.parseInt(
  process.env.ASTRABOX_E2E_BG_LOST_CHILD_SLEEP_SECONDS || '20',
  10,
);

// The part the hold parks on. For a backgrounded Agent call this is the CLI's
// own `async_launched` tool result — the first tool output the prompt below
// permits the parent to produce.
const HELD_FRAME_TYPE = 'tool-output-available';

// The vendor's own marker for a finished background task in its SessionStore.
const TASK_NOTIFICATION_MARKER = '<task-notification>';
// Claude Code keeps each Agent's conversation in a transcript of its own
// (`engine/claude_code_background.py`).
const AGENT_TRANSCRIPT_PREFIX = 'subagents/agent-';

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Launch one detached child that leaves two marks in the workspace.
 *
 * STARTED proves the child is really running while the host loses its
 * connection; DONE proves it finished its work in the live box. Without the
 * second one, "the user never heard about it" would be a statement about
 * nothing.
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
    `When you are notified that the background Agent finished, reply with one short sentence containing ${marker}_DONE and call no tool.`,
  ].join('\n');
}

/** The input id the inputs route answered a composer send with. */
async function acceptedInputId(response: Response): Promise<string> {
  const body = (await response.json()) as { data?: { input_id?: unknown } };
  const inputId = String(body.data?.input_id ?? '').trim();
  expect(inputId, 'the inputs route must answer with the accepted input id').not.toBe('');
  return inputId;
}

// ── Claude's own transcript, as the SessionStore mirror holds it ───────────
//
// One row per CLI JSONL line (`transcript_entries`). The main transcript is the
// parent's conversation; each Agent has its own `subagents/agent-<id>` file.

interface NativeEntry {
  seq: number;
  entry: Record<string, unknown>;
}

function nativeEntries(sessionId: string, scope: 'main' | 'agents'): NativeEntry[] {
  return nativeTranscriptRows(sessionId)
    .filter((row) => (scope === 'main'
      ? row.subpath === null
      : String(row.subpath ?? '').startsWith(AGENT_TRANSCRIPT_PREFIX)))
    .map((row) => ({ seq: Number(row.seq), entry: JSON.parse(String(row.entry_json)) as Record<string, unknown> }));
}

/** A line of the parent's own lane, not a sub-agent's line carried in its file. */
function isParentLane(entry: Record<string, unknown>): boolean {
  if (entry.isSidechain === true) return false;
  return String(entry.parentToolUseId ?? entry.parent_tool_use_id ?? '').trim() === '';
}

function contentBlocks(entry: Record<string, unknown>): Record<string, unknown>[] {
  const message = entry.message;
  if (!message || typeof message !== 'object') return [];
  const content = (message as Record<string, unknown>).content;
  return Array.isArray(content)
    ? content.filter((block): block is Record<string, unknown> => Boolean(block) && typeof block === 'object')
    : [];
}

/** A line's visible text, block by block; a string `message.content` is one block. */
function textBlocks(entry: Record<string, unknown>): string[] {
  const message = entry.message;
  if (!message || typeof message !== 'object') return [];
  const content = (message as Record<string, unknown>).content;
  if (typeof content === 'string') return [content];
  return contentBlocks(entry)
    .filter((block) => block.type === 'text' && typeof block.text === 'string')
    .map((block) => String(block.text));
}

/** What the parent said, in order, in its main-lane assistant lines after `afterSeq` and before `beforeSeq`. */
function parentSpeech(entries: NativeEntry[], afterSeq: number, beforeSeq = Number.POSITIVE_INFINITY): string {
  return entries
    .filter((item) => item.seq > afterSeq && item.seq < beforeSeq)
    .filter((item) => isParentLane(item.entry) && item.entry.type === 'assistant')
    .flatMap((item) => textBlocks(item.entry))
    .join('');
}

/** The parent's main-lane user lines carrying `marker`, in order. */
function parentUserLines(entries: NativeEntry[], marker: string): NativeEntry[] {
  return entries.filter((item) => isParentLane(item.entry) && item.entry.type === 'user'
    && textBlocks(item.entry).some((text) => text.includes(marker)));
}

/** The parent's backgrounded Agent calls for this run, with Claude's launch receipt for each. */
function nativeLaunches(sessionId: string, marker: string): Array<{ toolUseId: string; agentId: string }> {
  const entries = nativeEntries(sessionId, 'main').filter((item) => isParentLane(item.entry));
  const calls = entries.flatMap((item) => contentBlocks(item.entry))
    .filter((block) => block.type === 'tool_use' && ['Agent', 'Task'].includes(String(block.name))
      && (block.input as Record<string, unknown> | undefined)?.run_in_background === true
      && JSON.stringify(block.input).includes(marker));
  return calls.map((call) => {
    const receipt = entries.find((item) => {
      const result = item.entry.toolUseResult as Record<string, unknown> | undefined;
      return result?.status === 'async_launched'
        && contentBlocks(item.entry).some((block) => block.type === 'tool_result' && block.tool_use_id === call.id);
    });
    const result = receipt?.entry.toolUseResult as Record<string, unknown> | undefined;
    return { toolUseId: String(call.id ?? ''), agentId: String(result?.agentId ?? '') };
  });
}

/** The tool results the child recorded for its own Bash calls. */
function childBashResults(sessionId: string): Record<string, unknown>[] {
  const blocks = nativeEntries(sessionId, 'agents').flatMap((item) => contentBlocks(item.entry));
  const calls = new Set(blocks.filter((block) => block.type === 'tool_use' && block.name === 'Bash')
    .map((block) => String(block.id ?? '')));
  return blocks.filter((block) => block.type === 'tool_result' && calls.has(String(block.tool_use_id ?? '')));
}

/** Is this workspace file readable with this exact content? */
async function sentinelReadable(
  api: AstraApi,
  platform: PlatformApi,
  sessionId: string,
  root: string,
  path: string,
  expected: string,
): Promise<boolean> {
  const name = path.split('/').at(-1);
  const listing = await platform.listFiles(sessionId, root, 15_000);
  if (!listing.entries?.some((entry) => entry.name === name && entry.kind === 'file')) {
    return false;
  }
  return (await api.downloadFileText(sessionId, path, 15_000)).trim() === expected;
}

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene — session, its records, sandbox Pod — and names it in the report tail.
const sessions = trackSessions();

let openSessionId = '';
onPassOnly(async ({ request }) => {
  // A conversation whose connection was taken may leave the engine mid-flight;
  // only a passing run may clear it, so a failure keeps every live handle.
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

  expect(
    Number.isSafeInteger(CHILD_SLEEP_SECONDS) && CHILD_SLEEP_SECONDS > 0,
    'ASTRABOX_E2E_BG_LOST_CHILD_SLEEP_SECONDS must be a positive whole number of seconds',
  ).toBe(true);

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
    'this journey is written in Claude Code\'s background vocabulary (run_in_background, '
      + 'async_launched, task-notification; engine/claude_code_background.py). Select a Claude '
      + 'Code profile for this lane (--research-agent / ASTRABOX_E2E_AGENT_NAME), or leave this '
      + 'file out of the selection on a campaign that runs another engine.',
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
  const root = String((await platform.listFiles(sessionId)).root_path || '').trim();
  expect(root, 'the files listing must name the workspace root the child writes into').not.toBe('');
  const startedPath = `${root}/.astrabox-e2e-bg-lost-${runId}.started`;
  const donePath = `${root}/.astrabox-e2e-bg-lost-${runId}.done`;

  // Armed BEFORE the first prompt: the hold parks the reader on the launch
  // receipt, and a declaration written after the send would race it.
  const frameHoldPath = frameHoldFaultPath(sessionId);
  armFrameHoldFault(frameHoldPath, sessionId, HELD_FRAME_TYPE);
  test.info().annotations.push({ type: 'e2e_turn_frame_hold_file', description: frameHoldPath });

  const sentInputIds: string[] = [];
  let launchResponseId = '';

  try {
    // The user watches their own conversation for the whole outage.
    await page.setViewportSize({ width: 1280, height: 720 });
    await openSessionView(page, sessionId);

    // ── The send is a real one, from the composer. ─────────────────────────
    const prompt = backgroundAgentPrompt(runId, marker, startedPath, donePath);
    sentInputIds.push(await acceptedInputId(await sendPrompt(page, sessionId, prompt)));
    await expect(
      page.getByTestId('user-message').last(),
      'the user bubble must render — proof the launcher was dispatched from the page',
    ).toContainText(marker, { timeout: 30_000 });

    // ── Reach the window: the reader parked on the launch receipt, and a
    //    child already running behind it. Ask again rather than skip — a
    //    runtime skip ends the round exactly as a failure does, and decides
    //    coverage by the model's mood. ────────────────────────────────────────
    await insist<true>({
      ask: async (attempt) => {
        if (attempt > 1) sentInputIds.push((await api.postTurnInput(sessionId, prompt)).input_id);
      },
      probe: async () => {
        const deadline = Date.now() + LAUNCH_MS;
        while (Date.now() < deadline) {
          const session = await api.getSession(sessionId);
          if (['TERMINATED', 'DELETED'].includes(String(session.state || ''))) {
            throw new Error(
              `session ${sessionId} reached ${session.state} while waiting for the launch; `
                + `last_error=${session.last_error || '<none>'}`,
            );
          }
          const held = frameHoldConsumed(frameHoldPath, sessionId, HELD_FRAME_TYPE);
          if (held && await sentinelReadable(api, platform, sessionId, root, startedPath, 'started')) {
            return true;
          }
          // A reply that ended with nothing held and no child is a decline the
          // second ask can fix; everything else waits out the probe.
          const latest = sentInputIds.at(-1)!;
          const answered = sessionResponseRows(sessionId)
            .some((row) => row.input_ids.includes(latest) && row.status === 'ended');
          if (!held && answered) return null;
          await sleep(2_000);
        }
        throw new Error(
          `the launch window never opened within ${LAUNCH_MS}ms. `
            + `fault_consumed=${JSON.stringify(readFrameHoldFault(frameHoldPath)?.consumed ?? null)}; `
            + `child_runs=${JSON.stringify((await api.listChildRuns(sessionId)).child_runs)}. `
            + 'If the declaration was never consumed, this deployment is not running with '
            + `ASTRABOX_E2E_FAULTS=1 or ${frameHoldPath} is not the path the backend reads. `
            + 'If it WAS consumed and no child started, the parent reached for another tool '
            + 'first and the hold parked ahead of the launch.',
        );
      },
      what: 'the model answered without launching a run_in_background Agent — this spec\'s '
        + 'subject only exists once a detached child is running',
      budgetMs: LAUNCH_MS,
      probeMs: LAUNCH_MS,
    });
    const launchInputId = sentInputIds.at(-1)!;

    // ── The launch reply is still running: the hold is inside it. ───────────
    const beforeLoss = await api.getSession(sessionId);
    const launching = sessionResponseRows(sessionId).filter((row) => row.input_ids.includes(launchInputId));
    expect(
      launching.map((row) => ({ origin: row.origin, status: row.status })),
      'the reply answering the launch must be one running response — the hold is inside it',
    ).toEqual([{ origin: 'input', status: 'running' }]);
    launchResponseId = launching[0]!.response_id;
    expect(
      (beforeLoss.current_responses ?? []).map((response) => response.response_id),
      'the session must report the launch reply as the response in progress',
    ).toEqual([launchResponseId]);
    expect(
      await sentinelReadable(api, platform, sessionId, root, donePath, 'done'),
      'the child must still have work to finish after the runtime connection is evicted',
    ).toBe(false);

    // ── THE LOSS: the host's connection to this session's runtime, and only that.
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

    // Let the parked reader go, into a connection the eviction already closed.
    releaseFrameHoldFault(frameHoldPath);
    // Claude's own record of the launch the platform must keep tracking: one
    // backgrounded Agent call with its async_launched receipt.
    await expect.poll(() => nativeLaunches(sessionId, marker).filter((launch) => launch.agentId).length, {
      timeout: 15_000,
      message: 'Claude\'s own transcript must hold one backgrounded Agent call with its async_launched receipt',
    }).toBe(1);

    // ── The user sends nothing else. The launch reply has to end, as written.
    const launchReply = await api.waitForReplyTo(sessionId, launchInputId, { timeoutMs: SETTLE_MS });
    expect(launchReply.id).toBe(launchResponseId);
    expect(
      launchReply.metadata?.outcome,
      'the launch reply finished in the runtime; losing the connection to it must not fail it',
    ).toBe('completed');
    // The task notification can quote the child's prompt, so it is not one of these.
    const launchPrompts = parentUserLines(nativeEntries(sessionId, 'main'), `E2E background launcher loss ${runId}.`)
      .filter((line) => !textBlocks(line.entry).some((text) => text.includes(TASK_NOTIFICATION_MARKER)));
    expect(launchPrompts.length, 'Claude\'s transcript must hold each prompt this spec sent').toBe(sentInputIds.length);
    await expect.poll(() => {
      const main = nativeEntries(sessionId, 'main');
      const notice = parentUserLines(main, TASK_NOTIFICATION_MARKER)[0];
      return parentSpeech(main, launchPrompts.at(-1)!.seq, notice?.seq).trim();
    }, {
      timeout: 15_000,
      message: 'the launch reply in history must be exactly what Claude wrote — none lost, none twice',
    }).toBe(messageText(launchReply).trim());

    // ── ANTI-VACUITY, asserted before anything about tracking: the work
    //    really happened, in the live box, after the host lost its connection.
    await expect
      .poll(() => sentinelReadable(api, platform, sessionId, root, donePath, 'done'), {
        timeout: COMPLETION_MS,
        intervals: [1_000, 2_000],
        message:
          `the background child must finish its work in the live box (${donePath}). `
          + 'If it never does, the child died with the host connection — a different defect '
          + 'from the one this spec is about, and the assertions below would be reports '
          + 'about nothing.',
      })
      .toBe(true);

    // ── (1) The platform tracked the one child it launched, to its end. ─────
    const closed = await api.waitForChildRuns(
      sessionId,
      (rows) => rows.length >= 1 && rows.every((row) => row.closed),
      COMPLETION_MS,
    );
    expect(
      closed.map((row) => ({ engine_kind: row.engine_kind, closed: row.closed, engine_status: row.engine_status })),
      'exactly one child run, closed with the engine\'s own completed status carried verbatim',
    ).toEqual([{ engine_kind: 'claude_code', closed: true, engine_status: 'completed' }]);
    expect(
      JSON.stringify(childBashResults(sessionId)),
      'the child\'s own transcript must hold its Bash result with the receipt it printed',
    ).toContain(`${marker}_DONE`);

    // ── (2) Claude's answer to the finished child reaches the conversation,
    //    with nobody attached: one response it started on its own. ──────────
    await expect
      .poll(
        () => sessionResponseRows(sessionId)
          .filter((row) => row.origin === 'engine')
          .map((row) => ({ status: row.status, outcome: row.outcome, input_ids: row.input_ids })),
        {
          timeout: COMPLETION_MS,
          intervals: [1_000, 2_000],
          message: 'Claude must answer the finished child with exactly one response of its own',
        },
      )
      .toEqual([{ status: 'ended', outcome: 'completed', input_ids: [] }]);
    const engineResponseId = sessionResponseRows(sessionId).find((row) => row.origin === 'engine')!.response_id;
    const history = await api.getMessages(sessionId, 50);
    const answer = history.messages.find((message) => message.id === engineResponseId);
    expect(answer?.metadata?.origin, 'history must show the runtime\'s own answer as its own message').toBe('engine');
    const main = nativeEntries(sessionId, 'main');
    const notices = parentUserLines(main, TASK_NOTIFICATION_MARKER);
    expect(notices, 'Claude\'s transcript must hold the one task notification it answered').toHaveLength(1);
    const nativeAnswer = parentSpeech(main, notices[0]!.seq).trim();
    expect(nativeAnswer, 'Claude must have said something in answer to the finished child').not.toBe('');
    expect(messageText(answer!).trim(), 'history must hold Claude\'s answer exactly as Claude wrote it').toBe(nativeAnswer);
    expect(
      sessionInputRows(sessionId).map((row) => row.input_id),
      'the completion must arrive without a message from anyone',
    ).toEqual(sentInputIds);

    // ── (3) The user comes back to the conversation and reads the result.
    //    A cold open: the page rebuilds its child-run registry from the
    //    Session resource, which is the read path anyone checking on a
    //    background result actually takes. ──────────────────────────────────
    await openSessionView(page, sessionId);
    await expect(
      page.getByTestId('user-message').filter({ hasText: marker }),
      'the launcher message the user sent must still be in the transcript',
    ).toHaveCount(sentInputIds.length, { timeout: 30_000 });
    const answerOnPage = page.locator(`[data-message-id="${engineResponseId}"]`);
    await expect(answerOnPage, 'the runtime\'s own answer must be one message on the page').toHaveCount(1, {
      timeout: 30_000,
    });
    if (await answerOnPage.getByTestId('assistant-turn-process').count() > 0) {
      await revealAssistantProcess(page, { within: answerOnPage });
    }
    const rendered = answerOnPage.getByTestId('assistant-text');
    await expect
      .poll(async () => normalizeRendered((await rendered.allInnerTexts()).join('\n')), {
        timeout: 30_000,
        message: 'the transcript must show Claude\'s answer to the finished child',
      })
      .toBe(normalizeRendered(nativeAnswer));

    await page.getByRole('tab', { name: /^Agents/ }).click();
    const agentsPanel = page.getByTestId('subagent-agents-panel');
    await expect(agentsPanel).toBeVisible({ timeout: 15_000 });
    const row = agentsPanel.locator(`[data-child-run-id="${closed[0]!.child_run_id}"]`);
    await expect(row, 'the launched child must render a row in the Agents panel').toHaveCount(1, {
      timeout: 30_000,
    });
    // Exact match on the badge, not `hasText` on the row: a row's subtitle can
    // itself contain the word completed.
    await expect(
      row.getByText(/^completed$/),
      'the Agents panel must close the child out at the engine\'s exact status',
    ).toBeVisible({ timeout: 30_000 });

    // And the header settles. A conversation that kept its child but is left
    // pulsing tells the user work is still running.
    await expect(
      page.getByTestId('run-view').getByTestId('status-pill').first(),
      'a conversation that lost its connection must not be left pulsing',
    ).toHaveAttribute('data-pulse', 'false', { timeout: 30_000 });
    expect(
      sessionResponseRows(sessionId).filter((row) => row.origin === 'engine').map((row) => row.response_id),
      'reading the settled result must not start another answer',
    ).toEqual([engineResponseId]);

    console.log('BACKGROUND_LAUNCH_LOSS_E2E_EVIDENCE', JSON.stringify({
      session_id: sessionId,
      sandbox_id: sandboxId,
      launch_response_id: launchResponseId,
      engine_response_id: engineResponseId,
      held_frame_type: HELD_FRAME_TYPE,
      child_sentinels: { started: startedPath, done: donePath },
      child_runs: closed.map((childRun: ChildRunRecord) => ({
        child_run_id: childRun.child_run_id,
        engine_kind: childRun.engine_kind,
        engine_event: childRun.engine_event,
        engine_status: childRun.engine_status,
        closed: childRun.closed,
      })),
    }));
  } finally {
    // The declaration goes first and unconditionally. A hold nobody releases
    // raises on the backend after its own 120s timeout and stops the reader for
    // a reason unrelated to this spec, and a file left behind would park the
    // next response in this deployment. `trackSessions()` still decides whether
    // the session itself survives for diagnosis.
    releaseFrameHoldFault(frameHoldPath);
    clearFrameHoldFault(frameHoldPath);
    let responses: unknown;
    try {
      responses = sessionResponseRows(sessionId);
    } catch (error) {
      // A broken read must not replace the failure this run is unwinding from.
      responses = { unavailable: String(error) };
    }
    test.info().annotations.push({ type: 'e2e_session_responses', description: JSON.stringify(responses) });
  }
});
