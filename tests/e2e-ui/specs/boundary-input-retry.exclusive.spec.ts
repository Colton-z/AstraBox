/**
 * E2E: an input rejected before backend acceptance stays a retryable local
 * message, and its manual retry becomes the next prompt in order.
 *
 * Migrated from the internal case `agent_chat boundary input becomes the next
 * prompt with stable first-paint and retryable local failure`
 * (`f841a9161df083af7972c67cb5ace3fb60f75d1c`,
 * `tests/e2e/specs/agent-chat-lifecycle.exclusive.spec.ts:7071-7434`); the
 * design and assertion mapping are in
 * `docs/maintainers/boundary-input-retry-migration.md`.
 *
 * The first prompt holds a real Bash tool open on a release file in the shared
 * `/workspace`. While it runs, the browser route answers the second input's
 * first POST with an HTTP 503 it never forwards: the message must stay in the
 * composer queue as a failed, retryable row with its exact error, never
 * become a conversation row, and leave no backend command behind. The one
 * manual retry keeps the same client message id and is held at the route
 * until the supplier's own Result for the first prompt is durable; only then
 * is it forwarded. Its first paint must be immediately after the previous
 * response — never at the top — and the live and cold timelines must agree on
 * every row's identity and order.
 *
 * Community identities differ from the donor's and are read from the actual
 * records, never derived: the receipt names `command_id` and `input_id`, the
 * public user row is `${input_id}:user`, the response row is the
 * `response_message_id` the consumed boundary names, and the native Store row
 * is found by the exact prompt content the consumed boundary confirmed: the
 * runner maps the vendor uuid it minted for the delivery back to the platform
 * input id in memory, and no durable record relates the two.
 */
import { expect, test } from '@playwright/test';
import type { Page, Response, Route } from '@playwright/test';

import { AstraApi, messageText, visibleMessages } from '../fixtures/astraApi';
import { openLiveProcessGroup } from '../fixtures/assistantProcess';
import { documentsByField, framesForTurn, sessionEvents } from '../fixtures/dbOracle';
import { apiPath, parseTimeoutEnv } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, startPromptDelivery } from '../fixtures/sessionPage';
import { aiStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';

const TOOL_START_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_TOOL_START_TIMEOUT_MS', 90_000);
const TURN_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 120_000);
const FILE_NOT_FOUND = /404|FILE_NOT_FOUND|path not found/;
const FORCED_FAILURE_CODE = 'E2E_FORCED_TURN_INPUT_FAILURE';
const FORCED_FAILURE_MESSAGE = 'forced first native input failure';
const FORCED_FAILURE_TEXT = `${FORCED_FAILURE_CODE}: ${FORCED_FAILURE_MESSAGE}`;
const RETRY_QUEUED = /Retry queued message|重试排队消息/;

const sessions = trackSessions();

interface TimelineRow {
  id: string;
  kind: 'user' | 'assistant' | 'other';
  text: string;
}

interface InsertionSnapshot {
  rowId: string;
  index: number;
  rows: Array<{ id: string; kind: string }>;
}

interface Receipt {
  clientMessageId: string;
  commandId: string;
  inputId: string;
}

/** One stored native Store row with its parsed transcript line. */
interface NativeStoreRow {
  seq: number;
  entry: Record<string, unknown>;
  row: Record<string, unknown>;
}

interface Scene {
  sessionId: string;
  sourceTurnId: string;
  boundaryTurnId: string;
  attempts: number;
  heldReleased: boolean;
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`expected an object, received ${JSON.stringify(value)}`);
  }
  return value as Record<string, unknown>;
}

async function observe(read: () => unknown): Promise<unknown> {
  try { return { available: true, value: await read() }; }
  catch (error) { return { available: false, error: String(error) }; }
}

/**
 * The session's root native Store rows in sequence order. The row keeps the
 * platform stamps the in-box store wrote beside the entry; the entry is the
 * supplier's own JSONL line, parsed from `entry_json`.
 */
function nativeStoreRows(sessionId: string): NativeStoreRow[] {
  return documentsByField('transcript_entries', '$.platform_session_id', sessionId)
    .filter((row) => row.subpath == null)
    .map((row) => ({
      seq: Number(row.seq),
      entry: object(JSON.parse(String(row.entry_json))),
      row,
    }))
    .sort((a, b) => a.seq - b.seq);
}

// Mutated by the one test in this file; read by the failure hook below.
const scene: Scene = {
  sessionId: '', sourceTurnId: '', boundaryTurnId: '', attempts: 0, heldReleased: false,
};

test.afterEach(async ({ page, request }, info) => {
  if (!['failed', 'timedOut', 'interrupted'].includes(String(info.status))) return;
  if (!scene.sessionId) return;
  const api = new AstraApi(request);
  const evidence = await Promise.all([
    observe(() => api.getSession(scene.sessionId)),
    observe(() => api.getMessages(scene.sessionId, 100)),
    observe(() => sessionEvents(scene.sessionId)),
    observe(() => (scene.sourceTurnId ? framesForTurn(scene.sourceTurnId) : [])),
    observe(() => (scene.boundaryTurnId ? framesForTurn(scene.boundaryTurnId) : [])),
    observe(() => nativeStoreRows(scene.sessionId)),
    observe(() => aiStreamBodies(page)),
    observe(() => readInsertionRecords(page)),
    observe(() => readTimelineRows(page)),
  ]);
  await info.attach('boundary-input-retry-scene', {
    body: JSON.stringify({
      scene,
      session: evidence[0],
      history: evidence[1],
      session_events: evidence[2],
      source_turn_frames: evidence[3],
      boundary_turn_frames: evidence[4],
      native_store: evidence[5],
      browser_stream: evidence[6],
      insertion_records: evidence[7],
      timeline_rows: evidence[8],
    }, null, 2),
    contentType: 'application/json',
  });
});

/** The turn-input receipt, or a loud failure naming what the response carried. */
async function deliveredReceipt(response: Response, content: string): Promise<Receipt> {
  expect(response.status(), 'native turn-input POST should succeed').toBe(200);
  const body = await response.json() as { data?: Record<string, unknown>; [key: string]: unknown };
  const receipt = body.data ?? body;
  expect(receipt.status, 'backend should confirm delivery to the engine FIFO').toBe('delivered');
  const requestBody = response.request().postDataJSON() as { client_message_id?: string; content?: unknown };
  expect(requestBody.content, 'FIFO delivery must preserve the exact user content').toBe(content);
  const clientMessageId = String(receipt.client_message_id || requestBody.client_message_id || '').trim();
  const commandId = String(receipt.command_id || '').trim();
  const inputId = String(receipt.input_id || '').trim();
  expect(clientMessageId, 'FIFO delivery should preserve the client message id').not.toEqual('');
  expect(commandId, 'FIFO delivery should expose the durable command id').not.toEqual('');
  expect(inputId, 'FIFO delivery should expose the canonical engine input id').toMatch(/^[0-9a-f-]{36}$/);
  return { clientMessageId, commandId, inputId };
}

/**
 * Whether the held tool wrote its started marker. The Agent and the platform
 * terminal run in sibling isolated sessions whose `/tmp` mounts are private;
 * the conversation workspace and its file API are the shared surface.
 */
async function fileExists(api: AstraApi, sessionId: string, filePath: string): Promise<boolean> {
  try {
    await api.downloadFileText(sessionId, filePath, 30_000);
    return true;
  } catch (error) {
    if (FILE_NOT_FOUND.test(String((error as Error)?.message ?? error))) return false;
    throw error;
  }
}

/**
 * Wait for the first prompt's Bash tool to be running. A turn that settles
 * without reaching it is a failure of this case's setup, not a reason to ask
 * again: the donor sends one prompt and holds one Bash.
 */
async function waitForHeldTool(api: AstraApi, sessionId: string, startedPath: string): Promise<void> {
  const deadline = Date.now() + TOOL_START_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (await fileExists(api, sessionId, startedPath)) return;
    const detail = await api.getSession(sessionId);
    if (!detail.current_turn_id && String(detail.state || '') === 'READY' && detail.last_turn_id) {
      const history = visibleMessages(await api.getMessages(sessionId, 100));
      const last = history.filter((message) => message.role === 'assistant').map(messageText).at(-1) || '';
      throw new Error(
        'the source response Bash tool must pause before boundary submissions; the turn settled '
        + `as ${detail.last_turn_status} without it (last assistant text=${JSON.stringify(last)})`,
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  throw new Error(`the source response Bash tool did not start within ${TOOL_START_TIMEOUT_MS}ms`);
}

/** Every rendered transcript row, in DOM order, by its message wrapper. */
async function readTimelineRows(page: Page): Promise<TimelineRow[]> {
  return page
    .locator('[data-testid="session-conversation"] [data-message-id]')
    .evaluateAll((elements) => elements.map((element) => ({
      id: element.getAttribute('data-message-id') || '',
      kind: element.querySelector('[data-testid="user-message"]')
        ? 'user' as const
        : element.querySelector('[data-testid="assistant-message"]')
          ? 'assistant' as const
          : 'other' as const,
      text: Array.from(element.querySelectorAll('.markdown'))
        .map((node) => node.textContent || '')
        .join('')
        .trim(),
    })));
}

/**
 * Record, from inside the page, the timeline at the instant each user row
 * first enters the DOM. A driver-side poll only sees the DOM between polls;
 * a first paint at the wrong position that is corrected before the next poll
 * passes unseen. Keyed by the community message wrapper and test ids.
 */
async function installInsertionObserver(page: Page): Promise<void> {
  await page.evaluate(() => {
    type ObserverWindow = Window & typeof globalThis & {
      __astraboxBoundaryInsertion?: { observer: MutationObserver; records: InsertionSnapshot[] };
    };
    const target = window as ObserverWindow;
    target.__astraboxBoundaryInsertion?.observer.disconnect();
    const records: InsertionSnapshot[] = [];
    const seen = new Set<string>();
    const scan = () => {
      const rows = Array.from(
        document.querySelectorAll<HTMLElement>('[data-testid="session-conversation"] [data-message-id]'),
      ).map((element) => ({
        id: element.getAttribute('data-message-id') || '',
        kind: element.querySelector('[data-testid="user-message"]')
          ? 'user'
          : element.querySelector('[data-testid="assistant-message"]')
            ? 'assistant'
            : 'other',
      }));
      rows.forEach((row, index) => {
        if (row.kind !== 'user' || !row.id || seen.has(row.id)) return;
        seen.add(row.id);
        records.push({ rowId: row.id, index, rows });
      });
    };
    const observer = new MutationObserver(scan);
    observer.observe(document.documentElement, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['data-message-id', 'data-testid'],
    });
    target.__astraboxBoundaryInsertion = { observer, records };
    scan();
  });
}

async function readInsertionRecords(page: Page): Promise<InsertionSnapshot[]> {
  return page.evaluate(() => (
    (window as Window & typeof globalThis & {
      __astraboxBoundaryInsertion?: { records: InsertionSnapshot[] };
    }).__astraboxBoundaryInsertion?.records ?? []
  ));
}

async function waitForInsertion(page: Page, rowId: string, timeoutMs: number): Promise<InsertionSnapshot> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const record = (await readInsertionRecords(page)).find((item) => item.rowId === rowId);
    if (record) return record;
    await page.waitForTimeout(250);
  }
  throw new Error(`native input row ${rowId} was never observed entering the DOM`);
}

/** The accepted-but-unconsumed inputs the session detail publishes. */
function pendingInputs(detail: Record<string, unknown>): Array<Record<string, unknown>> {
  const value = detail.pending_inputs;
  return Array.isArray(value) ? value.map(object) : [];
}

/**
 * An event's payload object, or an empty one for an event that carries none.
 * Journal events are filtered by identity fields, so a payload-less event
 * simply fails to match; `object()` above stays for parsed native entries,
 * where a malformed line is itself a finding.
 */
function payloadOf(event: Record<string, unknown>): Record<string, unknown> {
  const payload = event.payload;
  return payload && typeof payload === 'object' && !Array.isArray(payload)
    ? payload as Record<string, unknown>
    : {};
}

/** The accepted command carrying one caller-supplied client message id. */
function commandsForClientMessage(sessionId: string, clientMessageId: string): Record<string, unknown>[] {
  return sessionEvents(sessionId).filter((event) => (
    event.event_type === 'command.accepted'
    && String(payloadOf(event).client_message_id || '').trim() === clientMessageId
  ));
}

/**
 * Every delivery-channel event of the session. `input.delivered` carries only
 * `input_id` and `sequence`, so a refused input is proved absent by the
 * channel not growing, not by searching it for content.
 */
function deliveryChannelEvents(sessionId: string): Record<string, unknown>[] {
  return sessionEvents(sessionId).filter((event) => (
    ['input.delivered', 'input.consumed'].includes(String(event.event_type))
  ));
}

/** Consumed boundaries whose engine or delivered content is one exact prompt. */
function consumedForContent(sessionId: string, content: string): Record<string, unknown>[] {
  return sessionEvents(sessionId).filter((event) => (
    event.event_type === 'input.consumed'
    && (payloadOf(event).content === content || payloadOf(event).sdk_content === content)
  ));
}

/** The `input.delivered` receipts the adapter wrote for one command. */
function deliveredReceipts(sessionId: string, commandId: string): Record<string, unknown>[] {
  return sessionEvents(sessionId).filter((event) => (
    event.event_type === 'input.delivered' && String(event.causation_id || '').trim() === commandId
  ));
}

/** The one `input.consumed` boundary for a command, or null while it has not happened. */
function consumedBoundary(sessionId: string, commandId: string): Record<string, unknown> | null {
  const consumed = sessionEvents(sessionId).filter((event) => (
    event.event_type === 'input.consumed' && String(event.causation_id || '').trim() === commandId
  ));
  expect(consumed.length, `command ${commandId} must be consumed at most once`).toBeLessThanOrEqual(1);
  return consumed[0] ?? null;
}

/** Durable Result frames of a turn written after one event sequence. */
function resultFramesAfter(turnId: string, afterSeq: number): number[] {
  return framesForTurn(turnId)
    .filter((frame) => String(object(frame.payload).type || '').trim() === 'data-result')
    .map((frame) => Number(frame.event_seq))
    .filter((seq) => Number.isInteger(seq) && seq > afterSeq);
}

/**
 * Whether a native entry belongs to the root conversation. Both spellings are
 * excluded: the CLI JSONL marks a subagent line `isSidechain`, the
 * runner-serialized envelope `parent_tool_use_id`.
 */
function isParentLane(entry: Record<string, unknown>): boolean {
  if (entry.isSidechain === true) return false;
  const parentToolUseId = entry.parentToolUseId ?? entry.parent_tool_use_id;
  return String(parentToolUseId ?? '').trim() === '';
}

/** The text of a native root user entry, or null when the entry is not one. */
function rootUserText(entry: Record<string, unknown>): string | null {
  if (entry.type !== 'user' || !isParentLane(entry)) return null;
  const content = object(entry.message).content;
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return null;
  const blocks = content.map(object);
  if (blocks.some((block) => block.type !== 'text')) return null;
  return blocks.map((block) => String(block.text || '')).join('\n');
}

interface NativeInputEvidence {
  entry_seq: number;
  entry_uuid: string;
  parent_uuid: string;
  command_id_stamp: string;
  platform_turn_id_stamp: string;
  assistant_descendants: Array<{ entry_seq: number; entry_uuid: string }>;
}

/**
 * The native Store row holding one exact root prompt, with its linked
 * assistant successors before another user input. Claude's parentUuid chain
 * includes attachment/system/progress entries, not just visible messages.
 * The runner mints a vendor uuid per delivery and maps
 * the engine's echo back to the platform input id in memory; the platform
 * then cross-checks the consumed content against the delivered content and
 * records it on the consumed boundary. No durable record relates the native
 * uuid to the platform input id, so the row is located by that confirmed
 * content. The row's platform stamps are recorded as evidence, not asserted:
 * the platform writes them and reads none of them back.
 */
function nativeInputEvidence(rows: NativeStoreRow[], content: string): { exact: number; evidence: NativeInputEvidence | null } {
  const exact = rows.filter(({ entry }) => rootUserText(entry) === content);
  if (exact.length === 0) return { exact: 0, evidence: null };
  // Evidence is built from the first match even when there are several, so a
  // duplicated prompt is reported with what was stored, not as an absence.
  const [{ seq, entry, row }] = exact;
  const uuid = String(entry.uuid || '').trim();
  expect(uuid, 'the exact native user entry must have its vendor uuid').not.toEqual('');
  const linked = new Set([uuid]);
  const assistants: NativeInputEvidence['assistant_descendants'] = [];
  for (const candidate of rows) {
    if (candidate.seq <= seq || !isParentLane(candidate.entry) || candidate.entry.type === 'user') continue;
    const candidateUuid = String(candidate.entry.uuid || '').trim();
    const parentUuid = String(candidate.entry.parentUuid || '').trim();
    if (!candidateUuid || !linked.has(parentUuid)) continue;
    linked.add(candidateUuid);
    if (candidate.entry.type === 'assistant') {
      assistants.push({ entry_seq: candidate.seq, entry_uuid: candidateUuid });
    }
  }
  return {
    exact: exact.length,
    evidence: {
      entry_seq: seq,
      entry_uuid: uuid,
      parent_uuid: String(entry.parentUuid || ''),
      command_id_stamp: String(row.command_id || ''),
      platform_turn_id_stamp: String(row.platform_turn_id || ''),
      assistant_descendants: assistants,
    },
  };
}

interface DurableEvidence {
  command_id: string;
  client_message_id: string;
  input_id: string;
  turn_id: string;
  response_message_id: string;
  delivered_sequence: number;
  consumed_event_seq: number;
  consumer_carrier: string;
  result_sequences: number[];
  native: NativeInputEvidence;
  history_user_index: number;
  history_assistant_indices: number[];
}

interface DurableObservation {
  delivered: Record<string, unknown>[];
  consumed: Record<string, unknown> | null;
  consumedPayload: Record<string, unknown>;
  consumedSeq: number;
  consumedTurnId: string;
  resultSequences: number[];
  native: ReturnType<typeof nativeInputEvidence>;
  historyUserIndices: number[];
  historyAssistantIndices: number[];
}

async function observeDurable(
  api: AstraApi,
  sessionId: string,
  submission: Receipt,
  content: string,
): Promise<DurableObservation> {
  const delivered = deliveredReceipts(sessionId, submission.commandId);
  const consumed = consumedBoundary(sessionId, submission.commandId);
  const consumedPayload = consumed ? payloadOf(consumed) : {};
  const consumedSeq = consumed ? Number(consumed.event_seq) : -1;
  const consumedTurnId = String(consumed?.turn_id || '').trim();
  const resultSequences = consumedTurnId ? resultFramesAfter(consumedTurnId, consumedSeq) : [];
  const native = nativeInputEvidence(nativeStoreRows(sessionId), content);
  const history = (await api.getMessages(sessionId, 100)).messages;
  const historyUserIndices = history.flatMap((message, index) => (
    message.role === 'user'
    && String(message.message_id || '').trim() === `${submission.inputId}:user`
    && messageText(message).trim() === content.trim()
      ? [index]
      : []
  ));
  const historyUserIndex = historyUserIndices[0] ?? -1;
  const historyAssistantIndices = history.flatMap((message, index) => (
    message.role === 'assistant' && historyUserIndex >= 0 && index > historyUserIndex ? [index] : []
  ));
  return {
    delivered, consumed, consumedPayload, consumedSeq, consumedTurnId, resultSequences, native,
    historyUserIndices, historyAssistantIndices,
  };
}

/**
 * Wait until the retried input has reached every durable boundary the donor
 * required, then judge each one on its own: the adapter's delivery receipt,
 * the accepted command naming it, the engine consuming exactly it once, the
 * supplier's Result after that, the native Store holding its exact prompt
 * once with an assistant successor, and public history showing one user row
 * followed by an answer. The wait is only for arrival; exactness is asserted
 * afterwards so a duplicate or a diverging content fails on its own diff
 * instead of as a timeout.
 */
async function waitForBoundaryInputDurable(
  api: AstraApi,
  sessionId: string,
  submission: Receipt,
  content: string,
  timeoutMs: number,
): Promise<DurableEvidence> {
  const accepted = commandsForClientMessage(sessionId, submission.clientMessageId);
  expect(accepted, 'delivery must record exactly one accepted command for the retried client id').toHaveLength(1);
  const acceptedPayload = payloadOf(accepted[0]);
  expect(String(accepted[0].causation_id || ''), 'the accepted command must be the delivered command id').toBe(submission.commandId);
  expect(String(acceptedPayload.input_id || ''), 'the accepted command must carry the delivered input id').toBe(submission.inputId);
  expect(acceptedPayload.content, 'the accepted command must carry the exact prompt').toBe(content);
  const turnId = String(accepted[0].turn_id || '').trim();
  expect(turnId, 'the accepted command must belong to a platform turn').not.toEqual('');
  scene.boundaryTurnId = turnId;

  const deadline = Date.now() + timeoutMs;
  let seen = await observeDurable(api, sessionId, submission, content);
  const arrived = (observation: DurableObservation) => (
    observation.delivered.length > 0
    && observation.consumed !== null
    && observation.resultSequences.length > 0
    && observation.native.evidence !== null
    && observation.native.evidence.assistant_descendants.length > 0
    && observation.historyUserIndices.length > 0
    && observation.historyAssistantIndices.length > 0
  );
  while (!arrived(seen) && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 1_000));
    seen = await observeDurable(api, sessionId, submission, content);
  }
  if (!arrived(seen)) {
    throw new Error(
      'the retried input did not reach its delivered, consumed, native and durable boundaries; last='
      + JSON.stringify({
        delivered: seen.delivered.length,
        consumed_found: seen.consumed !== null,
        consumed_turn_id: seen.consumedTurnId,
        result_sequences: seen.resultSequences,
        native_exact_users: seen.native.exact,
        native_assistant_descendants: seen.native.evidence?.assistant_descendants.length ?? 0,
        history_user_indices: seen.historyUserIndices,
        history_assistant_indices: seen.historyAssistantIndices,
      }),
    );
  }

  expect(seen.delivered, 'the adapter must record exactly one delivery receipt for the command').toHaveLength(1);
  expect(String(payloadOf(seen.delivered[0]).input_id || ''), 'the delivery receipt must name the delivered input').toBe(submission.inputId);
  expect(String(seen.consumedPayload.input_id || ''), 'consumption must name the delivered input').toBe(submission.inputId);
  expect(String(seen.consumedPayload.client_message_id || ''), 'consumption must retain the client message id').toBe(submission.clientMessageId);
  expect(seen.consumedPayload.content, 'consumption must carry the delivered content').toBe(content);
  expect(seen.consumedPayload.sdk_content, 'the engine must consume the exact delivered content').toBeUndefined();
  expect(seen.native.exact, 'the native Store must hold the exact prompt once').toBe(1);
  expect(seen.historyUserIndices, 'public history must show the input once').toHaveLength(1);
  return {
    command_id: submission.commandId,
    client_message_id: submission.clientMessageId,
    input_id: submission.inputId,
    turn_id: seen.consumedTurnId,
    response_message_id: String(seen.consumedPayload.response_message_id || ''),
    delivered_sequence: Number(payloadOf(seen.delivered[0]).sequence),
    consumed_event_seq: seen.consumedSeq,
    consumer_carrier: String(seen.consumedPayload.consumer_carrier || ''),
    result_sequences: seen.resultSequences,
    native: seen.native.evidence!,
    history_user_index: seen.historyUserIndices[0],
    history_assistant_indices: seen.historyAssistantIndices,
  };
}

test('a boundary input rejected before acceptance retries as the next prompt with a stable first paint', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  scene.sessionId = sessionId;
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });

  const runId = `${Date.now()}-${test.info().workerIndex}`;
  const toolStartedPath = `/workspace/.astrabox-e2e-boundary-retry-${runId}.started`;
  const toolReleasePath = `/workspace/.astrabox-e2e-boundary-retry-${runId}.release`;
  const forcedToolCommand = [
    "python3 - <<'PY'",
    'import time',
    'from pathlib import Path',
    `started = Path(${JSON.stringify(toolStartedPath)})`,
    `release = Path(${JSON.stringify(toolReleasePath)})`,
    "started.write_text('started', encoding='utf-8')",
    'deadline = time.monotonic() + 180',
    'while not release.is_file():',
    '    if time.monotonic() >= deadline:',
    "        raise RuntimeError('native boundary E2E tool release timed out')",
    '    time.sleep(0.1)',
    "print('native boundary E2E tool released')",
    'PY',
  ].join('\n');
  const sourcePrompt = [
    'Use the Bash tool exactly once to execute the following command verbatim:',
    '```bash',
    forcedToolCommand,
    '```',
    'Do not use Skill or any other tool.',
    'Wait for the Bash result, then briefly report that it finished.',
  ].join('\n');
  const boundaryMarker = `NATIVE_AFTER_RESULT_${runId}`;
  const boundaryPrompt = [
    `Boundary retry ${boundaryMarker}.`,
    'Do not use tools. Answer this as the next prompt.',
  ].join(' ');
  const turnInputsPath = apiPath(`/sessions/${sessionId}/turn-inputs`);
  const routePattern = `**${turnInputsPath}`;
  const isTurnInputPost = (response: Response) => (
    response.url().includes(turnInputsPath) && response.request().method() === 'POST'
  );
  let heldRoute: Route | null = null;
  let retryResponsePromise: Promise<Response> | null = null;

  await api.waitForSessionReady(sessionId);
  await api.setPermissionMode(sessionId, 'bypassPermissions');
  await mirrorSseBodies(page);
  await openSessionView(page, sessionId);
  const statusPill = page.getByTestId('run-view').locator('header').getByTestId('status-pill');
  await expect(statusPill, 'the conversation must be ready before the source prompt').toHaveText(/Ready|就绪/, { timeout: 30_000 });

  try {
    // 1. One real first prompt whose single Bash tool waits on the release file.
    const sourceDelivery = await startPromptDelivery(page, sessionId, sourcePrompt);
    const source = await deliveredReceipt(await sourceDelivery.response, sourcePrompt);
    expect(source.clientMessageId).toBe(sourceDelivery.clientMessageId);
    await waitForHeldTool(api, sessionId, toolStartedPath);

    // Both submissions must originate from an active engine response.
    await expect(statusPill, 'both submissions must originate from an active SDK response')
      .toHaveAttribute('data-state', 'PROCESSING', { timeout: 30_000 });
    // A running turn keeps its tool cards inside a process group that starts
    // closed, so the card is readable only once the reader opens the group.
    await openLiveProcessGroup(page);
    await expect(
      page.getByRole('button', { name: /Bash/ }).first(),
      'the held command reads as working while it waits on the release file',
    ).toHaveText(/Working|处理中/, { timeout: 30_000 });

    // The source input's consumed boundary and the response it opened, from
    // the actual delivery records rather than from an identity formula.
    await expect.poll(() => consumedBoundary(sessionId, source.commandId) !== null, {
      timeout: 30_000,
      intervals: [500, 1_000],
      message: 'the active response must have one consumed root input',
    }).toBe(true);
    const sourceConsumed = consumedBoundary(sessionId, source.commandId);
    const sourceConsumedPayload = object(sourceConsumed!.payload);
    expect(String(sourceConsumedPayload.input_id || '')).toBe(source.inputId);
    const sourceConsumedSeq = Number(sourceConsumed!.event_seq);
    const sourceTurnId = String(sourceConsumed!.turn_id || '').trim();
    const sourceResponseId = String(sourceConsumedPayload.response_message_id || '').trim();
    expect(sourceTurnId, 'the consumed source input must name its platform turn').not.toEqual('');
    expect(sourceResponseId, 'the active SDK response must expose its stable id').not.toEqual('');
    scene.sourceTurnId = sourceTurnId;
    const sourceInputRowId = `${source.inputId}:user`;
    await expect(
      page.locator(`[data-message-id="${sourceInputRowId}"] [data-testid="user-message"]`),
      'the source SDK input must be on screen under its stable id',
    ).toHaveCount(1, { timeout: 30_000 });
    await expect(
      page.locator(`[data-message-id="${sourceResponseId}"] [data-testid="assistant-message"]`),
      'the active SDK response must be on screen under its stable id',
    ).toHaveCount(1, { timeout: 30_000 });
    // The DOM row is fed by the stream and the overlay by journaled frames;
    // they converge, so the cross-check is polled rather than read once.
    await expect.poll(async () => (
      visibleMessages(await api.getMessages(sessionId, 100))
        .filter((message) => message.role === 'assistant' && message.message_id === sourceResponseId)
        .map((message) => message.turn_id)
    ), {
      timeout: 15_000,
      intervals: [500, 1_000],
      message: 'the platform must publish the active response under the same id and turn',
    }).toEqual([sourceTurnId]);
    expect(resultFramesAfter(sourceTurnId, sourceConsumedSeq), 'the source Result must not exist while Bash is held').toEqual([]);
    const deliveryBaseline = deliveryChannelEvents(sessionId).length;

    // 2. Intercept only this session's second input. The first attempt is
    // refused at the browser and never forwarded; the second is held.
    await page.route(routePattern, async (route) => {
      if (route.request().method() !== 'POST') {
        await route.continue();
        return;
      }
      scene.attempts += 1;
      if (scene.attempts === 1) {
        await route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ code: FORCED_FAILURE_CODE, message: FORCED_FAILURE_MESSAGE, data: null }),
        });
        return;
      }
      if (scene.attempts === 2) {
        heldRoute = route;
        return;
      }
      await route.continue();
    });

    const composer = page.getByTestId('composer-prompt');
    await expect(composer, 'a busy conversation still accepts the boundary input').toBeEnabled({ timeout: 30_000 });
    await composer.fill(boundaryPrompt);
    const failedResponsePromise = page.waitForResponse(isTurnInputPost, { timeout: 45_000 });
    await page.getByTestId('composer-submit').click();
    const failedResponse = await failedResponsePromise;
    expect(failedResponse.status(), 'the first native input is intentionally failed').toBe(503);
    const failedQueue = page.getByTestId('composer-queue');
    await expect(
      failedQueue,
      'a request rejected before backend acceptance must remain in the local queue',
    ).toBeVisible({ timeout: 30_000 });
    await expect(failedQueue).toContainText(/Queue 1|队列 1/);
    await expect(failedQueue).toContainText(/Send failed|发送失败/);
    await expect(failedQueue).toContainText(boundaryMarker);
    await expect(
      page.getByText(FORCED_FAILURE_TEXT, { exact: true }),
      'a rejected native input must expose its exact error immediately',
    ).not.toHaveCount(0, { timeout: 30_000 });
    await expect(
      page.getByTestId('user-message').filter({ hasText: boundaryMarker }),
      'a rejected input must not masquerade as a sent conversation row',
    ).toHaveCount(0);
    const failedRequest = failedResponse.request().postDataJSON() as {
      client_message_id?: string;
      content?: unknown;
    };
    const failedClientMessageId = String(failedRequest.client_message_id || '').trim();
    expect(failedRequest.content).toBe(boundaryPrompt);
    expect(failedClientMessageId, 'the retryable local failure must retain its client id').not.toEqual('');
    await expect(
      failedQueue.getByRole('button', { name: RETRY_QUEUED }),
      'a request rejected before backend acceptance must remain retryable',
    ).toBeEnabled();
    expect(
      commandsForClientMessage(sessionId, failedClientMessageId),
      'the browser-injected failure must not create backend delivery state',
    ).toEqual([]);
    expect(deliveryChannelEvents(sessionId).length, 'nothing may be delivered for a refused input').toBe(deliveryBaseline);
    expect(consumedForContent(sessionId, boundaryPrompt), 'the engine must not have consumed a refused prompt').toEqual([]);
    expect(
      pendingInputs(await api.getSession(sessionId)).map((input) => String(input.client_message_id || '')),
      'the platform must not hold a pending input it never accepted',
    ).not.toContain(failedClientMessageId);

    // 3. Watch first paints, then retry and hold the retry before acceptance.
    await installInsertionObserver(page);
    retryResponsePromise = page.waitForResponse(isTurnInputPost, { timeout: TURN_TIMEOUT_MS });
    await failedQueue.getByRole('button', { name: RETRY_QUEUED }).click();
    const heldAt = Date.now();
    await expect.poll(() => heldRoute !== null, {
      timeout: 45_000,
      intervals: [100, 250],
      message: 'the second busy submission must be held at the turn-input boundary',
    }).toBe(true);
    const heldRequest = heldRoute!.request().postDataJSON() as { client_message_id?: string; content?: unknown };
    expect(String(heldRequest.client_message_id || '').trim(), 'retry must resend the same client message id').toBe(failedClientMessageId);
    expect(heldRequest.content, 'retry must resend the same content').toBe(boundaryPrompt);
    await expect(failedQueue).toContainText(/Sending|发送中/, { timeout: 30_000 });
    await expect(statusPill, 'the header stays live while the retry is held').toHaveAttribute('data-state', 'PROCESSING');
    await expect(
      page.getByTestId('user-message').filter({ hasText: boundaryMarker }),
      'retrying the local queue must not insert a main conversation row before SDK consumption',
    ).toHaveCount(0);
    expect(commandsForClientMessage(sessionId, failedClientMessageId), 'a held request has reached no backend').toEqual([]);
    expect(deliveryChannelEvents(sessionId).length, 'a held request has delivered nothing').toBe(deliveryBaseline);
    expect(
      String((await api.getSession(sessionId)).current_turn_id || '').trim(),
      'the source response must still be the live turn while the retry is held',
    ).toBe(sourceTurnId);

    // 4. Release the real Bash; require the supplier's actual Result for the
    // source input, and the source response still on screen, before the held
    // retry reaches the backend. A settled READY state is not that evidence.
    // The terminal's own output echoes the command, so the release file is
    // proved through the file API the started marker already used.
    await api.runTerminalCommand(sessionId, `touch ${toolReleasePath}`, '/tmp', 30_000);
    expect(
      await fileExists(api, sessionId, toolReleasePath),
      'the release file must exist after the terminal wrote it — the blocking tool waits on this exact path',
    ).toBe(true);
    await expect.poll(() => resultFramesAfter(sourceTurnId, sourceConsumedSeq).length, {
      timeout: 90_000,
      intervals: [500, 1_000],
      message: 'the source SDK ResultMessage must arrive before the held input is accepted',
    }).toBeGreaterThan(0);
    const sourceResultSequences = resultFramesAfter(sourceTurnId, sourceConsumedSeq);
    await expect.poll(
      async () => (await readTimelineRows(page)).some((row) => row.id === sourceResponseId && row.kind === 'assistant'),
      {
        timeout: 60_000,
        intervals: [500, 1_000],
        message: 'the previous response must be visible before the held request reaches backend',
      },
    ).toBe(true);

    expect(heldRoute, 'the second request must remain held after the previous response').not.toBeNull();
    const holdMs = Date.now() - heldAt;
    await heldRoute!.continue();
    scene.heldReleased = true;
    const retryResponse = await retryResponsePromise;

    // 5. The delivered receipt keeps the identity the failed attempt had.
    const submission = await deliveredReceipt(retryResponse, boundaryPrompt);
    expect(submission.clientMessageId, 'retry must preserve the original client message identity')
      .toBe(failedClientMessageId);
    const boundaryRowId = `${submission.inputId}:user`;

    const insertion = await waitForInsertion(page, boundaryRowId, 90_000);
    expect(insertion.rowId).toBe(boundaryRowId);
    expect(insertion.index, 'the next prompt must never first-paint at the top').toBeGreaterThan(0);
    expect(
      insertion.rows[insertion.index - 1],
      'an input accepted after settle must first-paint immediately after the previous response',
    ).toMatchObject({ id: sourceResponseId, kind: 'assistant' });

    // 6. One user row, its queue entry gone, and its answer directly after it.
    // The virtualized list wraps each message container in its own index
    // node, so adjacency is read from the rendered row order rather than a
    // CSS sibling selector; the answer container is then located by the exact
    // response id the consumed boundary named for this input.
    const nativeRow = page.locator(`[data-message-id="${boundaryRowId}"]`);
    await expect(nativeRow).toHaveCount(1, { timeout: 60_000 });
    await expect(
      page.getByTestId('composer-queue'),
      'the retried queue entry must disappear when the SDK consumes it',
    ).toHaveCount(0, { timeout: 60_000 });
    // The page projects the row from the live consumed frame; the journal's
    // `input.consumed` row is the same boundary's durable write.
    await expect.poll(() => consumedBoundary(sessionId, submission.commandId) !== null, {
      timeout: 30_000,
      intervals: [500, 1_000],
      message: 'a projected user row must have its consumed boundary',
    }).toBe(true);
    const boundaryConsumed = consumedBoundary(sessionId, submission.commandId);
    const boundaryResponseId = String(object(boundaryConsumed!.payload).response_message_id || '').trim();
    expect(boundaryResponseId, 'the consumed boundary must name the response it opened').not.toEqual('');
    await expect.poll(
      async () => {
        const rows = await readTimelineRows(page);
        const index = rows.findIndex((row) => row.kind === 'user' && row.id === boundaryRowId);
        const next = index < 0 ? null : rows[index + 1] ?? null;
        return next ? { id: next.id, kind: next.kind } : null;
      },
      {
        timeout: TURN_TIMEOUT_MS,
        intervals: [500, 1_000],
        message: 'the row immediately after the next prompt must be its own response',
      },
    ).toEqual({ id: boundaryResponseId, kind: 'assistant' });
    const nextResponseTail = page.locator(`[data-message-id="${boundaryResponseId}"]`);
    await expect(nextResponseTail.locator('[data-testid="assistant-message"]')).toHaveCount(1);
    await expect.poll(
      async () => (await nextResponseTail.locator('.markdown').allTextContents()).some((text) => text.trim().length > 0),
      {
        timeout: TURN_TIMEOUT_MS,
        intervals: [500, 1_000],
        message: 'the next prompt response must render after its exact SDK marker',
      },
    ).toBe(true);

    const liveRows = await readTimelineRows(page);
    const sourceResponseIndex = liveRows.findIndex((row) => row.kind === 'assistant' && row.id === sourceResponseId);
    const nativeIndex = liveRows.findIndex((row) => row.kind === 'user' && row.id === boundaryRowId);
    const nextResponseIndex = liveRows.findIndex((row, index) => index > nativeIndex && row.kind === 'assistant');
    expect(sourceResponseIndex).toBeGreaterThanOrEqual(0);
    expect(nativeIndex, 'the next prompt marker must follow the completed response').toBeGreaterThan(sourceResponseIndex);
    expect(nextResponseIndex, 'the next prompt response must follow its marker').toBeGreaterThan(nativeIndex);
    expect(nextResponseIndex, 'the next prompt response must be the row right after its marker').toBe(nativeIndex + 1);
    expect(liveRows[nextResponseIndex].id).toBe(boundaryResponseId);
    expect(
      liveRows.filter((row) => row.kind === 'user' && row.text.includes(boundaryMarker)),
      'the retried prompt must be on screen exactly once',
    ).toHaveLength(1);

    const durable = await waitForBoundaryInputDurable(api, sessionId, submission, boundaryPrompt, 90_000);
    expect(durable.response_message_id, 'durable consumption must name the response the page showed').toBe(boundaryResponseId);
    const settled = await api.waitForSession(
      sessionId,
      (detail) => (
        detail.state === 'READY'
        && !detail.current_turn_id
        && pendingInputs(detail).length === 0
      ),
      TURN_TIMEOUT_MS,
    );
    expect(settled.last_error, 'the boundary prompt must settle without an error').toBeFalsy();
    expect(settled.last_turn_status).toBe('COMPLETED');
    await expect(statusPill, 'the accepted second input must not remain permanently sending')
      .toHaveText(/Ready|就绪/, { timeout: 60_000 });
    await expect(page.getByText(FORCED_FAILURE_TEXT, { exact: true })).toHaveCount(0);
    await expect(page.getByText(/^(Queue 1|队列 1)$/)).toHaveCount(0);
    expect(scene.attempts, 'one refused attempt and one manual retry reach the route').toBe(2);

    // 7. The complete cold timeline equals the live one: every row's identity
    // and role, in order, through the same virtualized surface. The live
    // shape is read here, immediately before the reload, not from the earlier
    // order check: rows settle between the two.
    const liveShape = (await readTimelineRows(page)).map((row) => ({ id: row.id, kind: row.kind }));
    expect(
      liveShape.some((row) => row.kind === 'user' && row.id === boundaryRowId),
      'live timeline must contain the exact native input row before reload',
    ).toBe(true);
    const publicRoles = (await api.getMessages(sessionId, 100)).messages.map((message) => message.role);
    expect(
      liveShape.filter((row) => row.kind !== 'other').map((row) => row.kind),
      'the live timeline must show every public row in public order',
    ).toEqual(publicRoles);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(
      page.locator(`[data-message-id="${boundaryRowId}"] [data-testid="user-message"]`),
      'durable reload must restore the exact native input row',
    ).toHaveCount(1, { timeout: 60_000 });
    await expect.poll(
      async () => (await readTimelineRows(page)).map((row) => ({ id: row.id, kind: row.kind })),
      {
        timeout: 60_000,
        intervals: [250, 500, 1_000],
        message: 'durable timeline row ids and order must equal the live first-paint timeline',
      },
    ).toEqual(liveShape);
    await expect(page.getByTestId('composer-queue')).toHaveCount(0);
    await expect(page.getByTestId('composer-prompt')).toBeEnabled({ timeout: 30_000 });

    test.info().annotations.push({
      type: 'e2e_native_input_result_boundary',
      description: JSON.stringify({
        session_id: sessionId,
        source: { ...source, turn_id: sourceTurnId, response_message_id: sourceResponseId, consumed_event_seq: sourceConsumedSeq, result_sequences: sourceResultSequences },
        command_id: submission.commandId,
        client_message_id: submission.clientMessageId,
        input_id: submission.inputId,
        first_dom_index: insertion.index,
        first_dom_rows: insertion.rows.map((row) => row.id),
        route_attempts: scene.attempts,
        hold_ms: holdMs,
        durable,
      }),
    });
  } finally {
    // A held request that was never forwarded is aborted so the page does not
    // outlive the test waiting on it. The Bash tool is NOT released here: a
    // failing run keeps its scene, and the tool's own deadline ends it.
    const pending = heldRoute as Route | null;
    if (pending && !scene.heldReleased) {
      await pending.abort().catch(() => undefined);
    }
    // An aborted hold rejects the response wait nobody is awaiting any more.
    void retryResponsePromise?.catch(() => undefined);
    await page.unroute(routePattern).catch(() => undefined);
  }
});
