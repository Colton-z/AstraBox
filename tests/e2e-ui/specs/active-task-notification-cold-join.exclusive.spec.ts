/**
 * E2E: joining a session cold while a task-notification continuation is active
 * shows the one external prompt and nothing the engine owns.
 *
 * The scene is the engine's, not the platform's. One background Agent is
 * launched by the single external prompt; the launch turn returns immediately
 * and the conversation goes foreground-idle. The page is left THEN, so the whole
 * background window happens with no reader attached. The harness releases the
 * child's real Bash, so Claude Code's own `<task-notification>` is delivered to
 * a conversation that has no external input pending. The parent answers that
 * notification by running a second real Bash, which this spec holds open — that
 * held command IS the window, and the browser joins the session cold inside it.
 *
 * The window is opened on the vendor's own evidence, not on the harness's: the
 * native SessionStore mirror must hold one real `<task-notification>` naming the
 * completed child, and the parent's Bash `tool_use` at a later native position
 * with no result yet, while that command is observably running in the sandbox.
 *
 * What must hold in that window: the platform holds exactly the one external
 * prompt as an input, and the durable history carries it byte-for-byte under
 * its own client_message_id; the continuation is the session's running
 * response, which Claude started on its own and which answers no input; and
 * the header reads the run as active rather than settled. A notification is
 * not a platform input and never consumes one, so no response the session
 * publishes, and nothing the cold browser reads, names an input other than the
 * prompt. Releasing the gate finishes the continuation — the held tool_use gets
 * its real native result and the reply lands after it — and a full reload
 * still shows the same single user message.
 *
 * The two Bash calls rendezvous through sandbox-local sentinel files rather
 * than sleeps, so the window is opened and closed by this spec and not by a
 * number chosen to be long enough.
 */
import { expect, test } from '@playwright/test';
import type { Page, Response } from '@playwright/test';

import {
  AstraApi,
  messageText,
  visibleMessages,
  type ChildRunRecord,
  type InputReceipt,
  type MessageRecord,
  type SessionRecord,
} from '../fixtures/astraApi';
import {
  nativeTranscriptRows,
  sessionInputRows,
  sessionOutputRows,
  sessionResponseRows,
} from '../fixtures/dbOracle';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, startPromptDelivery, expectPromptDelivered } from '../fixtures/sessionPage';
import { sessionStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';

// The launch turn's own reply, and the continuation reply after the gate opens.
const TURN_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 180_000);
// How long a real Bash gate may take to be observed running in the sandbox.
const GATE_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_BACKGROUND_PROBE_TIMEOUT_MS', 90_000);
// Settling the background child and, later, the whole conversation.
const SETTLED_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_BACKGROUND_SETTLED_TIMEOUT_MS', 240_000);

// In-box ceilings bound a retained failing scene without releasing either gate
// from failure cleanup. Successful execution releases each gate in the test.
const CHILD_GATE_CEILING_SECONDS = 120;
const PARENT_GATE_CEILING_SECONDS = 100;

// The vendor's own marker for a task notification, in its SessionStore and in
// the browser alike. Records are counted by exactly this text.
const TASK_NOTIFICATION_MARKER = '<task-notification>';

const TERMINAL_STATES = new Set(['TERMINATED', 'DELETED']);

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * A python gate: stamp a sentinel with a unique marker, wait for the release
 * file, then print the completion marker.
 *
 * The sentinel carries a marker rather than existing empty because the probe
 * reads it with `cat`: a file check whose answer is the word "yes" is also
 * satisfied by an echo of the command that asked.
 */
function gateCommand(options: {
  sentinelPath: string;
  sentinelMarker: string;
  releasePath: string;
  ceilingSeconds: number;
  donePrint: string;
  timeoutMessage: string;
}): string {
  return [
    "python3 - <<'PY'",
    'from pathlib import Path',
    'import time',
    `Path(${JSON.stringify(options.sentinelPath)}).write_text(${JSON.stringify(options.sentinelMarker)})`,
    `release = Path(${JSON.stringify(options.releasePath)})`,
    `deadline = time.monotonic() + ${options.ceilingSeconds}`,
    'while not release.exists():',
    '    if time.monotonic() >= deadline:',
    `        raise TimeoutError(${JSON.stringify(options.timeoutMessage)})`,
    '    time.sleep(0.2)',
    `print(${JSON.stringify(options.donePrint)})`,
    'PY',
  ].join('\n');
}

/**
 * The sentinel's own marker, read out of the sandbox rather than inferred.
 *
 * The command succeeds whether or not the sentinel exists, so "not started yet"
 * is an ordinary empty answer. Nothing is caught here: a transport failure or a
 * 5xx from the terminal endpoint is a broken probe, and swallowing it would
 * report the sandbox as quiet when the question never reached it.
 */
async function gateIsRunning(
  api: AstraApi,
  sessionId: string,
  sentinelPath: string,
  sentinelMarker: string,
): Promise<boolean> {
  const output = await api.runTerminalCommand(
    sessionId,
    `cat ${sentinelPath} 2>/dev/null || true`,
    '/workspace',
    30_000,
  );
  return output.includes(sentinelMarker);
}

/**
 * The held background Agent keeps the runtime PROCESSING after the foreground
 * reply ends. Its open task is work even when no response or interaction is open.
 */
async function waitForForegroundIdle(
  api: AstraApi,
  sessionId: string,
  timeoutMs: number,
): Promise<SessionRecord> {
  const deadline = Date.now() + timeoutMs;
  let last: SessionRecord | null = null;
  while (Date.now() < deadline) {
    last = await api.getSession(sessionId);
    const state = String(last.state || '');
    if (
      state === 'PROCESSING'
      && (last.current_responses ?? []).length === 0
      && !last.pending_interaction
    ) return last;
    if (TERMINAL_STATES.has(state)) {
      throw new Error(
        `session ${sessionId} reached terminal ${state} while the launch turn was settling; `
        + `last=${JSON.stringify(last)}`,
      );
    }
    await sleep(1_500);
  }
  throw new Error(
    `session ${sessionId} did not return to foreground-idle within ${timeoutMs}ms; `
    + `last=${JSON.stringify(last)}`,
  );
}

async function waitForAssistantTextMatching(
  api: AstraApi,
  sessionId: string,
  pattern: RegExp,
  timeoutMs: number,
): Promise<MessageRecord> {
  const deadline = Date.now() + timeoutMs;
  let lastTexts: string[] = [];
  while (Date.now() < deadline) {
    const page = await api.getMessages(sessionId, 50);
    const messages = visibleMessages(page);
    lastTexts = messages
      .filter((message) => message.role === 'assistant')
      .map((message) => messageText(message).slice(0, 200));
    const hit = messages.find(
      (message) => message.role === 'assistant' && pattern.test(messageText(message)),
    );
    if (hit) return hit;
    await sleep(1_500);
  }
  throw new Error(
    `session ${sessionId} produced no assistant text matching ${pattern} within ${timeoutMs}ms; `
    + `assistant texts=${JSON.stringify(lastTexts)}`,
  );
}

/** The receipt of a message the page sent, checked against the id the page minted for it. */
async function receiptOf(response: Response): Promise<InputReceipt> {
  const receipt = (await response.json() as { data: InputReceipt }).data;
  const sent = response.request().postDataJSON() as { client_message_id?: string };
  expect(receipt.client_message_id, 'the receipt names the message the page sent').toBe(sent.client_message_id);
  return receipt;
}

/** The input ids a `start` part names; a part that reopens a paused message names none. */
function startedInputIds(part: Record<string, unknown>): unknown {
  const metadata = part.messageMetadata;
  return metadata && typeof metadata === 'object' ? (metadata as Record<string, unknown>).input_ids : undefined;
}

/**
 * Every COMPLETE SSE frame the cold-joined browser read on its session channel.
 *
 * A mirrored body can end mid-line, so the last element of the split is dropped
 * — that one is the only line whose incompleteness is expected. Every other
 * `data:` line is a finished frame, and one that does not parse is reported
 * rather than skipped: a parser that answers "no frames" to malformed bytes
 * would make the absence assertion below pass on a broken stream.
 */
async function coldStreamFrames(page: Page): Promise<Record<string, unknown>[]> {
  const bodies = await sessionStreamBodies(page);
  return bodies
    .flatMap((body) => body.text.split('\n').slice(0, -1))
    .filter((line) => line.startsWith('data: ') && line.trim() !== 'data: [DONE]')
    .map((line) => {
      try {
        return JSON.parse(line.slice(6)) as Record<string, unknown>;
      } catch (error) {
        throw new Error(
          `the cold session stream carried an unparsable complete frame: ${line.slice(0, 300)} `
          + `(${String(error)})`,
        );
      }
    });
}

// ── The native SessionStore mirror ─────────────────────────────────────────
//
// The vendor's own transcript, as the platform durably holds it: one row per
// SDK JSONL line in `transcript_entries`, under the file it belongs to in
// `transcript_scopes`, with the raw line in `entry_json`. The admin export route
// is not usable here — a session with a subagent answers with a gzip tar, and
// this spec always has one — so the rows are read through the database oracle
// (`nativeTranscriptRows`).
//
// Only the main scope (`subpath === null`) is the parent's conversation; the
// child Agent has its own `subagents/agent-*` file.

interface NativeEntry {
  seq: number;
  entry: Record<string, unknown>;
}

function mainScopeEntries(sessionId: string): NativeEntry[] {
  return nativeTranscriptRows(sessionId)
    .filter((row) => row.subpath === null)
    .map((row) => ({
      seq: Number(row.seq),
      entry: JSON.parse(String(row.entry_json)) as Record<string, unknown>,
    }))
    .sort((left, right) => left.seq - right.seq);
}

/**
 * Whether an entry belongs to the parent's own lane.
 *
 * A subagent's lines can appear among the parent's. Both of Claude's spellings
 * of that are excluded: the CLI JSONL marks it `isSidechain`, the
 * runner-serialized envelope `parent_tool_use_id`.
 */
function isParentLane(entry: Record<string, unknown>): boolean {
  if (entry.isSidechain === true) return false;
  const parentToolUseId = entry.parentToolUseId ?? entry.parent_tool_use_id;
  return String(parentToolUseId ?? '').trim() === '';
}

function messageContentBlocks(entry: Record<string, unknown>): Record<string, unknown>[] {
  const message = entry.message;
  if (!message || typeof message !== 'object') return [];
  const content = (message as Record<string, unknown>).content;
  if (!Array.isArray(content)) return [];
  return content.filter(
    (block): block is Record<string, unknown> => Boolean(block) && typeof block === 'object',
  );
}

/**
 * The visible text of a native line, block by block: a string
 * `message.content` counts as one text block, a list contributes its string
 * and `text`-typed entries.
 */
function messageTextBlocks(entry: Record<string, unknown>): string[] {
  const message = entry.message;
  if (!message || typeof message !== 'object') return [];
  const content = (message as Record<string, unknown>).content;
  if (typeof content === 'string') return [content];
  if (!Array.isArray(content)) return [];
  const texts: string[] = [];
  for (const block of content) {
    if (typeof block === 'string') {
      texts.push(block);
      continue;
    }
    if (!block || typeof block !== 'object') continue;
    const typed = block as Record<string, unknown>;
    if (String(typed.type ?? '').trim().toLowerCase() !== 'text') continue;
    if (typeof typed.text === 'string') texts.push(typed.text);
  }
  return texts;
}

function entryRole(entry: Record<string, unknown>): string {
  const entryType = String(entry.type ?? '').trim().toLowerCase();
  const message = entry.message;
  const role = message && typeof message === 'object'
    ? String((message as Record<string, unknown>).role ?? '').trim().toLowerCase()
    : '';
  return role || entryType;
}

/**
 * Consumed root USER records carrying `marker`.
 *
 * This is the fact counted, and it counts one: the CLI wrote a root user record
 * into its own SessionStore for the completed task. It is deliberately NOT the
 * union of every native line that mentions a notification. The CLI also
 * records a `queue-operation` note for the same delivery, and merging the two
 * would count queue records as consumed user records. Keep that distinction
 * without deduplicating either class.
 */
function consumedRootNotificationRecords(
  entries: NativeEntry[],
  marker: string,
): { seq: number; text: string }[] {
  const matches: { seq: number; text: string }[] = [];
  for (const item of entries) {
    if (!isParentLane(item.entry)) continue;
    const entryType = String(item.entry.type ?? '').trim().toLowerCase();
    if (entryType !== 'user' && entryRole(item.entry) !== 'user') continue;
    for (const text of messageTextBlocks(item.entry)) {
      if (text.includes(marker)) matches.push({ seq: item.seq, text });
    }
  }
  return matches;
}

/**
 * The CLI's own queueing notes for a delivery — reported, never counted as
 * notifications. The vendor may record one delivery in this shape as well as in
 * the root user record above; those two lines are one event.
 */
function queuedNotificationRecords(
  entries: NativeEntry[],
  marker: string,
): { seq: number; content: string }[] {
  return entries
    .filter((item) => isParentLane(item.entry))
    .filter((item) => String(item.entry.type ?? '').trim().toLowerCase() === 'queue-operation')
    .map((item) => ({ seq: item.seq, content: String(item.entry.content ?? '') }))
    .filter((item) => item.content.includes(marker));
}

/** The parent's own Bash call for `commandFragment`, by native tool_use id. */
function parentBashToolUse(
  entries: NativeEntry[],
  commandFragment: string,
): { seq: number; toolUseId: string } | undefined {
  for (const item of entries) {
    if (!isParentLane(item.entry)) continue;
    if (String(item.entry.type ?? '') !== 'assistant') continue;
    for (const block of messageContentBlocks(item.entry)) {
      if (String(block.type ?? '') !== 'tool_use') continue;
      if (String(block.name ?? '') !== 'Bash') continue;
      if (!JSON.stringify(block.input ?? null).includes(commandFragment)) continue;
      const toolUseId = String(block.id ?? '').trim();
      if (toolUseId) return { seq: item.seq, toolUseId };
    }
  }
  return undefined;
}

/** That same call's native result, still in the parent's lane. */
function parentBashToolResult(
  entries: NativeEntry[],
  toolUseId: string,
): { seq: number; block: Record<string, unknown> } | undefined {
  for (const item of entries) {
    if (!isParentLane(item.entry)) continue;
    for (const block of messageContentBlocks(item.entry)) {
      if (String(block.type ?? '') !== 'tool_result') continue;
      if (String(block.tool_use_id ?? '') !== toolUseId) continue;
      return { seq: item.seq, block };
    }
  }
  return undefined;
}

/** The parent's assistant text carrying `marker`, at its native position. */
function parentAssistantText(
  entries: NativeEntry[],
  marker: string,
): { seq: number } | undefined {
  for (const item of entries) {
    if (!isParentLane(item.entry)) continue;
    if (String(item.entry.type ?? '') !== 'assistant') continue;
    for (const block of messageContentBlocks(item.entry)) {
      if (String(block.type ?? '') !== 'text') continue;
      if (String(block.text ?? '').includes(marker)) return { seq: item.seq };
    }
  }
  return undefined;
}

const sessions = trackSessions();

test('cold join during a task-notification continuation keeps one external user message', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const rendezvous = `/workspace/.astrabox-e2e-task-notification-${runId}`;
  const childSentinel = `${rendezvous}.child-started`;
  const childRelease = `${rendezvous}.child-release`;
  const parentSentinel = `${rendezvous}.parent-started`;
  const parentRelease = `${rendezvous}.parent-release`;
  const childStartedMarker = `TASK_NOTIFICATION_CHILD_STARTED_${runId}`;
  const parentStartedMarker = `TASK_NOTIFICATION_PARENT_STARTED_${runId}`;
  const childDoneMarker = `TASK_NOTIFICATION_CHILD_${runId}_DONE`;
  const parentLaunchedMarker = `TASK_NOTIFICATION_PARENT_LAUNCHED_${runId}`;
  // What the held continuation command prints, and what the reply after it must
  // carry. Two markers: the command's own output is not the reply, and the
  // reply is the only thing the prompt asks the model for.
  const continuationDoneMarker = `TASK_NOTIFICATION_DELAY_${runId}_DONE`;
  const notificationHandledMarker = `TASK_NOTIFICATION_HANDLED_${runId}`;

  const childCommand = gateCommand({
    sentinelPath: childSentinel,
    sentinelMarker: childStartedMarker,
    releasePath: childRelease,
    ceilingSeconds: CHILD_GATE_CEILING_SECONDS,
    donePrint: childDoneMarker,
    timeoutMessage: 'the E2E harness did not release the background Agent',
  });
  const parentCommand = gateCommand({
    sentinelPath: parentSentinel,
    sentinelMarker: parentStartedMarker,
    releasePath: parentRelease,
    ceilingSeconds: PARENT_GATE_CEILING_SECONDS,
    donePrint: continuationDoneMarker,
    timeoutMessage: 'the E2E harness did not release the task-notification continuation',
  });

  // The one external input of the whole scenario. Everything after it is the
  // engine's own doing, so a second prompt — even to nudge the model — would
  // destroy the property under test. There is deliberately no `insist` re-ask
  // here for that reason: a model that declines to launch the Agent fails this
  // spec rather than being asked again.
  const prompt = [
    `E2E task-notification input boundary ${runId}. Follow every step literally.`,
    'Launch exactly one Agent tool call with run_in_background=true and subagent_type=general-purpose.',
    'The background Agent must call Bash exactly once, with this command verbatim:',
    childCommand,
    `Its final summary must contain ${childDoneMarker}.`,
    'In this first parent turn, do not call Bash, do not call TaskOutput, and do not wait for the Agent.',
    `Immediately after the Agent tool returns its task id, reply with one short sentence containing the token ${parentLaunchedMarker} copied verbatim.`,
    'Copy that token character for character. Do not rewrite it as prose, do not translate it, and do not replace its underscores with spaces.',
    `Only when the later task notification containing ${childDoneMarker} arrives, call Bash exactly once, with this command verbatim:`,
    parentCommand,
    `Wait for that Bash command to finish, then reply with one short sentence containing the token ${notificationHandledMarker} copied verbatim.`,
    'Do not create any of the release files yourself; the E2E harness owns them.',
  ].join('\n');

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });

  try {
    await api.waitForSessionReady(sessionId);
    // Both Bash calls have to run unattended; no tool-permission interaction may
    // stall the child or the continuation.
    await api.setPermissionMode(sessionId, 'bypassPermissions');

    // ── The single external prompt, sent through the real composer. ──────────
    await openSessionView(page, sessionId);
    const delivery = await startPromptDelivery(page, sessionId, prompt);
    const { input_id: inputId } = await receiptOf(await expectPromptDelivered(delivery));

    // ── The launch turn returns immediately, leaving the child running. ──────
    await waitForAssistantTextMatching(api, sessionId, new RegExp(parentLaunchedMarker), TURN_TIMEOUT_MS);
    const idle = await waitForForegroundIdle(api, sessionId, TURN_TIMEOUT_MS);
    expect(
      idle.last_response?.outcome,
      `the launch turn must settle before the notification arrives; detail=${JSON.stringify(idle)}`,
    ).toBe('completed');

    // ── Leave the session page here, before anything in the background window
    //    happens. Every step that follows — the child finishing, the vendor
    //    delivering its notification, the continuation starting — occurs with
    //    no reader attached, which is what makes the later join a cold one. ───
    await page.goto(appPath('/agents'), { waitUntil: 'domcontentloaded' });

    // The child's Bash is really running — the notification this spec waits for
    // will be the vendor's report of THIS command, not of a task that never ran.
    await expect
      .poll(() => gateIsRunning(api, sessionId, childSentinel, childStartedMarker), {
        timeout: GATE_TIMEOUT_MS,
        intervals: [1_000, 2_000],
        message: 'the background Agent must reach its real Bash gate before the notification is provoked',
      })
      .toBe(true);

    // ── Release the child. Its completion is what makes Claude Code queue the
    //    `<task-notification>` into a conversation with no external input. ────
    await api.runTerminalCommand(sessionId, `touch ${childRelease}`, '/workspace', 30_000);
    const rows: ChildRunRecord[] = await api.waitForChildRuns(
      sessionId,
      (candidates) => candidates.length === 1 && candidates[0]!.closed,
      SETTLED_TIMEOUT_MS,
    );
    const completedChild = rows[0]!;
    expect(
      completedChild,
      "exactly one background Agent must close with the engine's own completed status",
    ).toEqual(
      expect.objectContaining({
        closed: true,
        engine_kind: 'claude_code',
        engine_status: 'completed',
      }),
    );
    const childRunId = completedChild.child_run_id;

    // ── The continuation the notification started: a parent-only Bash, held
    //    open by this spec. Proven from the vendor's own durable transcript —
    //    one real `<task-notification>` naming the child's completion, then the
    //    parent's Bash tool_use at a LATER native position, with no result yet
    //    — and cross-checked against the command actually running in the box.
    //    A touched file is the harness's evidence of its own gate; it is not
    //    evidence that the engine called Bash, and neither is the sentinel
    //    alone evidence of whose lane called it. ────────────────────────────
    let heldBashToolUseId = '';
    await expect
      .poll(async () => {
        const entries = mainScopeEntries(sessionId);
        const consumed = consumedRootNotificationRecords(entries, TASK_NOTIFICATION_MARKER);
        const notification = consumed.find((item) => item.text.includes(childDoneMarker));
        if (!notification) return 'no consumed root task-notification naming the completed child yet';
        const bash = parentBashToolUse(entries, parentSentinel);
        if (!bash) return `notification landed at seq ${notification.seq}, parent Bash tool_use absent`;
        if (bash.seq <= notification.seq) {
          return `parent Bash at seq ${bash.seq} precedes the notification at seq ${notification.seq}`;
        }
        if (parentBashToolResult(entries, bash.toolUseId)) {
          return `parent Bash ${bash.toolUseId} already carries its result; the window has closed`;
        }
        if (!(await gateIsRunning(api, sessionId, parentSentinel, parentStartedMarker))) {
          return `parent Bash ${bash.toolUseId} is recorded but its command is not running`;
        }
        heldBashToolUseId = bash.toolUseId;
        return 'held';
      }, {
        timeout: GATE_TIMEOUT_MS,
        intervals: [1_000, 2_000],
        message:
          'the completed background Agent must start a parent-only Bash continuation: '
          + 'the vendor delivers `<task-notification>` to the resident conversation once the '
          + "child settles, and this spec's window is that continuation's held command",
      })
      .toBe('held');
    expect(heldBashToolUseId, 'the held continuation must have a native tool_use id').not.toEqual('');

    // One completed child, one consumed root notification record. Counted over
    // that class alone: the CLI's own queueing note for the same delivery is
    // recorded beside it and reported here, not added to the total.
    const windowEntries = mainScopeEntries(sessionId);
    test.info().annotations.push({
      type: 'e2e_native_task_notification_records',
      description: JSON.stringify({
        consumed_root: consumedRootNotificationRecords(windowEntries, TASK_NOTIFICATION_MARKER)
          .map((item) => item.seq),
        queue_operations: queuedNotificationRecords(windowEntries, TASK_NOTIFICATION_MARKER)
          .map((item) => item.seq),
      }),
    });
    expect(
      consumedRootNotificationRecords(windowEntries, TASK_NOTIFICATION_MARKER),
      'the one completed child must be reported by exactly one consumed root notification record',
    ).toHaveLength(1);

    // ── Join the session cold inside that window. ────────────────────────────
    await mirrorSseBodies(page);
    await openSessionView(page, sessionId);

    const coldDetail = await api.getSession(sessionId);
    const cold = await api.getMessages(sessionId, 50);
    test.info().annotations.push({
      type: 'e2e_active_task_notification_responses',
      description: JSON.stringify(coldDetail.current_responses ?? null),
    });

    // (1) The platform holds the one external prompt as its only input, under
    //     the identity the composer minted for it, and the durable history
    //     shows it and nothing else.
    expect(
      sessionInputRows(sessionId).map((row) => [row.input_id, row.client_message_id, row.content]),
      'a task notification is engine-owned; the platform holds only the submitted prompt as an input',
    ).toEqual([[inputId, delivery.clientMessageId, prompt]]);
    const durableUsers = visibleMessages(cold).filter((message) => message.role === 'user');
    expect(
      durableUsers.map(messageText),
      'a task notification is engine-owned; only the submitted prompt belongs to user history',
    ).toEqual([prompt]);
    expect(
      durableUsers[0]!.id,
      'the surviving user message must still be the composer submission, not a re-minted one',
    ).toEqual(delivery.clientMessageId);

    // (2) The continuation is the session's RUNNING response, and it is
    //     assistant-only: Claude started it on its own and it answers no input.
    const continuations = coldDetail.current_responses ?? [];
    expect(
      continuations.map((response) => [response.origin, response.status]),
      'an assistant-only task-notification continuation must be published as a running response, '
      + 'otherwise a cold join reads a working conversation as settled',
    ).toEqual([['engine', 'running']]);
    const continuationId = continuations[0]!.response_id;
    expect(
      sessionResponseRows(sessionId).filter((row) => row.response_id === continuationId),
      'the continuation must not be recorded as the answer to any input',
    ).toEqual([expect.objectContaining({ origin: 'engine', status: 'running', input_ids: [] })]);
    expect(
      cold.messages.filter((message) => message.id === continuationId)
        .map((message) => [message.role, message.metadata?.origin, message.metadata?.input_ids]),
      'history shows the running continuation as an assistant message answering no input',
    ).toEqual([['assistant', 'engine', []]]);

    // (3) The browser agrees: one bubble, and no notification rendered as one.
    await expect(
      page.getByTestId('user-message'),
      'cold join must render only the durable external prompt while the continuation is active',
    ).toHaveCount(1);
    await expect(
      page.getByTestId('user-message').filter({ hasText: TASK_NOTIFICATION_MARKER }),
      'cold bootstrap must not render the engine-owned task notification as a user bubble',
    ).toHaveCount(0);

    // (4) The header reads the run as active. Asserted on the machine-readable
    //     state, not the label: the label is translated.
    const statusPill = page.getByTestId('run-view').locator('header').getByTestId('status-pill');
    await expect(
      statusPill,
      'cold join must not label an active task-notification continuation as ready',
    ).toHaveAttribute('data-state', 'PROCESSING', { timeout: 30_000 });
    await expect(statusPill).toHaveAttribute('data-pulse', 'true');

    // ── Release the continuation and let it finish for real. ─────────────────
    await api.runTerminalCommand(sessionId, `touch ${parentRelease}`, '/workspace', 30_000);
    await waitForAssistantTextMatching(
      api,
      sessionId,
      new RegExp(notificationHandledMarker),
      TURN_TIMEOUT_MS,
    );

    // The held command really executed, and the reply came after its result.
    // The release file only opened the gate; what is asserted here is the
    // vendor's own tool_result for the exact tool_use this window was held on,
    // and the reply's position relative to it.
    await expect
      .poll(() => {
        const entries = mainScopeEntries(sessionId);
        const result = parentBashToolResult(entries, heldBashToolUseId);
        if (!result) return 'the held Bash has no native result yet';
        if (result.block.is_error === true) {
          return `the held Bash failed: ${JSON.stringify(result.block.content).slice(0, 200)}`;
        }
        if (!JSON.stringify(result.block.content ?? null).includes(continuationDoneMarker)) {
          return 'the held Bash result does not carry the command output';
        }
        const reply = parentAssistantText(entries, notificationHandledMarker);
        if (!reply) return 'the continuation has not replied yet';
        if (reply.seq <= result.seq) {
          return `the reply at seq ${reply.seq} precedes its own tool result at seq ${result.seq}`;
        }
        return 'answered after its result';
      }, {
        timeout: TURN_TIMEOUT_MS,
        intervals: [1_000, 2_000],
        message: 'the notification continuation must finish its real Bash and answer after it',
      })
      .toBe('answered after its result');

    const settled = await api.waitForSession(
      sessionId,
      (row) => String(row.state || '') === 'READY' && (row.current_responses ?? []).length === 0,
      SETTLED_TIMEOUT_MS,
    );
    expect(settled.pending_interaction ?? null, 'a finished continuation leaves nothing pending').toBeNull();
    expect(
      (await api.listChildRuns(sessionId)).child_runs,
      'the finished scenario still holds exactly the one completed background Agent',
    ).toEqual([
      expect.objectContaining({ child_run_id: childRunId, closed: true, engine_status: 'completed' }),
    ]);

    const completedHistory = await api.getMessages(sessionId, 50);
    expect(
      completedHistory.messages.filter((message) => message.role === 'assistant'
        && messageText(message).includes(notificationHandledMarker))
        .map((message) => [message.id, message.metadata?.origin, message.metadata?.outcome]),
      'the parent continuation must become one durable assistant answer, the response Claude started',
    ).toEqual([[continuationId, 'engine', 'completed']]);
    await expect(
      page.getByTestId('assistant-message').filter({ hasText: notificationHandledMarker }),
      'the already-open page must receive the continuation exactly once without a reload',
    ).toHaveCount(1);

    // (5) No consumption boundary was published, to this browser or to anyone.
    //     A response's `start` names the inputs it answers; across the session
    //     only the launch names one, the prompt. The cold page follows the
    //     stream from where its history left off, so it reads the rest of the
    //     running continuation: it is first shown to have read through that
    //     response's end, so "saw none" cannot mean "was not listening", and
    //     the absence is then over the frames it actually received.
    expect(
      sessionOutputRows(sessionId).map(({ part }) => part)
        .filter((part) => part.type === 'start' && part.messageMetadata)
        .map(startedInputIds)
        .filter((ids) => !Array.isArray(ids) || ids.length > 0),
      'a task notification is not a platform input: only the launch response consumed one',
    ).toEqual([[inputId]]);
    await expect
      .poll(async () => (await coldStreamFrames(page)).filter((frame) => frame.type === 'finish').length, {
        timeout: 30_000,
        intervals: [500, 1_000],
        message: 'the cold join must read the continuation off the session stream through its end '
          + 'before what it read can be asserted',
      })
      .toBeGreaterThan(0);
    expect(
      (await coldStreamFrames(page)).filter((frame) => frame.type === 'start' && frame.messageMetadata)
        .map(startedInputIds)
        .filter((ids) => !Array.isArray(ids) || ids.length > 0),
      'a task notification is not a platform input and must publish no consumption boundary',
    ).toEqual([]);

    // ── A full reload still shows the same single external user message. ─────
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('run-view')).toBeVisible();
    await expect(page.getByTestId('user-message')).toHaveCount(1);
    await expect(page.getByTestId('user-message')).toContainText(
      `E2E task-notification input boundary ${runId}.`,
    );
    await expect(
      page.getByTestId('user-message').filter({ hasText: TASK_NOTIFICATION_MARKER }),
    ).toHaveCount(0);
    const reloaded = await api.getMessages(sessionId, 50);
    expect(
      visibleMessages(reloaded).filter((message) => message.role === 'user').map(messageText),
      'the settled history must still carry the one external prompt, unchanged',
    ).toEqual([prompt]);
    expect(
      (await api.getSession(sessionId)).current_responses ?? [],
      'a settled conversation has no running response',
    ).toEqual([]);
    expect(
      reloaded.messages.filter((message: MessageRecord) => message.role === 'assistant'
        && message.metadata?.status !== 'ended'),
      'every reply in the settled history has ended',
    ).toEqual([]);
  } finally {
    // Preserve the actual state before afterEach can clean up a passing run.
    // Each failed read is evidence of unavailability, not an empty transcript.
    const probes = {
      session: () => api.getSession(sessionId),
      messages: () => api.getMessages(sessionId, 50),
      inputs: () => sessionInputRows(sessionId),
      responses: () => sessionResponseRows(sessionId),
      native_store: () => mainScopeEntries(sessionId),
      stream_bodies: () => sessionStreamBodies(page),
    };
    const evidence = await Promise.all(Object.entries(probes).map(async ([name, read]) => {
      try {
        return [name, { available: true, value: await read() }] as const;
      } catch (error) {
        return [name, { available: false, error: String(error) }] as const;
      }
    }));
    await test.info().attach('task-notification-final-observation', {
      contentType: 'application/json',
      body: JSON.stringify({
        observed_at: new Date().toISOString(),
        session_id: sessionId,
        gates: { childSentinel, childRelease, parentSentinel, parentRelease },
        ...Object.fromEntries(evidence),
      }),
    });
    // No interrupt, eviction or gate release here. trackSessions() decides
    // deletion after Playwright has recorded the real result.
  }
});
