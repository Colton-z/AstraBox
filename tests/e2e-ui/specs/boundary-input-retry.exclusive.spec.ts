/**
 * E2E: a message the session never received stays on screen as a failed,
 * retryable message, and its manual retry becomes the next prompt in order.
 *
 * The first prompt holds a real Bash tool open on a release file in the shared
 * `/workspace`. While its response runs, the browser route answers the second
 * message's first POST /inputs with an HTTP 503 it never forwards. The
 * console shows a message the moment it is sent, so that message stays in the
 * transcript right after the running response, marked as not sent with its
 * exact error and a Retry, and the platform records no input for it. The one
 * manual retry keeps the same client message id and is held at the route
 * until the first response has ended with the supplier's own Result; only
 * then is it forwarded. The message's first paint must be immediately after
 * the running response — never at the top — its answer must be the message
 * right after it, and the live and reloaded transcripts must agree on every
 * message's identity and order.
 *
 * Identities are read from the records, never derived: a user message's id is
 * its client message id, the receipt names the input id, the platform's
 * response row lists that input among the inputs it answers, and the native
 * transcript is searched for the exact prompt. The runtime's reader writes
 * the response rows and their parts (`session_responses`, `session_output`);
 * the supplier's own transcript is the runtime's mirrored store
 * (`transcript_entries`).
 */
import { execFileSync } from 'node:child_process';

import { expect, test } from '@playwright/test';
import type { Page, Response, Route } from '@playwright/test';

import { AstraApi, answersInput, messageText, sseFrames, visibleMessages, type InputReceipt } from '../fixtures/astraApi';
import { openLiveProcessGroup } from '../fixtures/assistantProcess';
import {
  postgresRows,
  sessionInputRows,
  sessionOutputRows,
  sessionResponseRows,
  sqlLiteral,
  type SessionInputRow,
  type SessionOutputRow,
  type SessionResponseRow,
} from '../fixtures/dbOracle';
import { apiPath, parseTimeoutEnv } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, startPromptDelivery } from '../fixtures/sessionPage';
import { SERVER_CONTAINER_HANDLE, requireServiceContainer } from '../fixtures/serviceContainer';
import { sessionStreamBodies, mirrorSseBodies } from '../fixtures/sseBodies';

const TOOL_START_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_TOOL_START_TIMEOUT_MS', 90_000);
const TURN_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 120_000);
const FILE_NOT_FOUND = /404|FILE_NOT_FOUND|path not found/;
const FORCED_FAILURE_CODE = 'E2E_FORCED_INPUT_FAILURE';
const FORCED_FAILURE_MESSAGE = 'forced first input failure';
// The console names a refusal by its code and message (`ApiError` in api.ts).
const FORCED_FAILURE_TEXT = `${FORCED_FAILURE_CODE}: ${FORCED_FAILURE_MESSAGE}`;
// misc:composer.retry and misc:composer.queue_sending, in both locales.
const RETRY = /^(Retry|重试)$/;
const SENDING = /^(Sending|发送中)$/;

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
  inputId: string;
}

/** One stored entry of the runtime's main transcript, with its parsed JSONL line. */
interface NativeStoreRow {
  scope: string;
  seq: number;
  entry: Record<string, unknown>;
}

interface Scene {
  sessionId: string;
  sourceResponseId: string;
  boundaryResponseId: string;
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
 * The session's main native transcript entries, scope by scope in sequence
 * order: the supplier's own JSONL lines as the runtime's store mirrored them
 * (`transcript_scopes.subpath` is empty for the main transcript).
 */
function nativeStoreRows(sessionId: string): NativeStoreRow[] {
  return postgresRows<{ scope: string; seq: number; entry_json: string }>(
    `SELECT s.native_session_id AS scope, e.seq, e.entry_json FROM transcript_entries e `
      + `JOIN transcript_scopes s ON s.scope_id = e.scope_id `
      + `WHERE s.platform_session_id = ${sqlLiteral(sessionId)} AND s.subpath = '' `
      + `ORDER BY s.native_session_id, e.seq`,
  ).map((row) => ({
    scope: String(row.scope),
    seq: Number(row.seq),
    entry: object(JSON.parse(String(row.entry_json))),
  }));
}

/** The platform's response row answering one input, or null while there is none. */
function responseFor(sessionId: string, inputId: string): SessionResponseRow | null {
  const answering = sessionResponseRows(sessionId).filter((row) => row.input_ids.includes(inputId));
  expect(answering.length, `input ${inputId} is answered by at most one response`).toBeLessThanOrEqual(1);
  return answering[0] ?? null;
}

function responseRow(sessionId: string, responseId: string): SessionResponseRow | null {
  return sessionResponseRows(sessionId).find((row) => row.response_id === responseId) ?? null;
}

/** The `data-result` parts a response's output carries: the supplier's own Result, translated. */
function resultParts(sessionId: string, responseId: string): SessionOutputRow[] {
  return sessionOutputRows(sessionId, responseId).filter((row) => row.part.type === 'data-result');
}

// Mutated by the one test in this file; read by the failure hook below.
const scene: Scene = {
  sessionId: '', sourceResponseId: '', boundaryResponseId: '', attempts: 0, heldReleased: false,
};

test.afterEach(async ({ page, request }, info) => {
  if (!['failed', 'timedOut', 'interrupted'].includes(String(info.status))) return;
  if (!scene.sessionId) return;
  const api = new AstraApi(request);
  const evidence = await Promise.all([
    observe(() => api.getSession(scene.sessionId)),
    observe(() => api.getMessages(scene.sessionId, 50)),
    observe(() => sessionInputRows(scene.sessionId)),
    observe(() => sessionResponseRows(scene.sessionId)),
    observe(() => (scene.sourceResponseId ? sessionOutputRows(scene.sessionId, scene.sourceResponseId) : [])),
    observe(() => (scene.boundaryResponseId ? sessionOutputRows(scene.sessionId, scene.boundaryResponseId) : [])),
    observe(() => nativeStoreRows(scene.sessionId)),
    observe(() => sessionStreamBodies(page)),
    observe(() => readInsertionRecords(page)),
    observe(() => readTimelineRows(page)),
  ]);
  await info.attach('boundary-input-retry-scene', {
    body: JSON.stringify({
      scene,
      session: evidence[0],
      history: evidence[1],
      inputs: evidence[2],
      responses: evidence[3],
      source_response_output: evidence[4],
      boundary_response_output: evidence[5],
      native_store: evidence[6],
      browser_stream: evidence[7],
      insertion_records: evidence[8],
      timeline_rows: evidence[9],
    }, null, 2),
    contentType: 'application/json',
  });
});

test('a persisted refusal survives a lost input receipt and its same-id retry', async ({ page, request }) => {
  // This selected case needs the existing runtime-preparation fixture provider.
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const { session_id: sessionId } = await api.startConversation(agent.agent_id);
  sessions.push(sessionId);
  Object.assign(scene, { sessionId, sourceResponseId: '', boundaryResponseId: '', attempts: 0, heldReleased: false });
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  test.info().annotations.push({ type: 'deployment_shape', description: 'runtime-preparation provider' });
  await api.waitForSessionReady(sessionId);
  await mirrorSseBodies(page);
  await openSessionView(page, sessionId);
  await expect(page.getByTestId('composer-prompt')).toBeEnabled();
  const beforeOutput = Number((await api.getSession(sessionId)).output_seq ?? 0);
  const path = apiPath(`/sessions/${sessionId}/inputs`);
  const routePattern = `**${path}`;
  const expectedFailure = {
    code: 'TURN_PREPARATION_USER_ACTION_REQUIRED',
    message: 'Authorize this Agent to continue',
    user_action: {
      status: 'user_action_required',
      message: 'Authorize this Agent to continue',
      url: 'https://example.com/authorize',
    },
  };
  // Use the Python preparation E2E's existing control, preserving other owners' entries.
  const control = (enabled: boolean) => execFileSync('docker', [
    'exec', server, 'python', '-c',
    'import json,sys; from pathlib import Path; '
      + "p=Path('/data/runtime-preparation-user-actions.json'); "
      + 'ids=json.loads(p.read_text()) if p.exists() else []; '
      + 'ids=[sid for sid in ids if sid!=sys.argv[1]]; '
      + "p.write_text(json.dumps(ids+[sys.argv[1]] if sys.argv[2]=='yes' else ids))",
    sessionId, enabled ? 'yes' : 'no',
  ], { timeout: 30_000, stdio: 'pipe' });
  let loseReceipt!: () => void;
  const receiptHeld = new Promise<void>((resolve) => { loseReceipt = resolve; });
  let accepted!: (receipt: InputReceipt) => void;
  let acceptanceFailed!: (error: unknown) => void;
  const firstAcceptance = new Promise<InputReceipt>((resolve, reject) => {
    accepted = resolve;
    acceptanceFailed = reject;
  });
  let refusalHandled: Promise<Response> | null = null;
  let retryReleased = false;
  try {
    control(true);
    await page.route(routePattern, async (route) => {
      if (route.request().method() !== 'POST') {
        await route.continue();
        return;
      }
      scene.attempts += 1;
      if (scene.attempts !== 1) {
        if (retryReleased) await route.continue();
        else await route.abort('failed');
        return;
      }
      try {
        const upstream = await route.fetch();
        expect(upstream.status(), 'the receipt is lost after real backend acceptance').toBe(200);
        accepted((await upstream.json()).data as InputReceipt);
        await receiptHeld;
        await route.abort('failed');
      } catch (error) {
        acceptanceFailed(error);
        throw error;
      }
    });
    // An idle page does not poll. Its refusal handler asks for this detail read;
    // wait for it as well as the actual frame before losing the POST receipt.
    refusalHandled = page.waitForResponse(async (response) => (
      new URL(response.url()).pathname === apiPath(`/sessions/${sessionId}`)
      && response.request().method() === 'GET'
      && response.status() === 200
      && Number((await response.json()).data.output_seq) > beforeOutput
    ));
    const prompt = `Without tools, briefly explain why leaves are green. Ref ${Date.now()}.`;
    await page.getByTestId('composer-prompt').fill(prompt);
    await page.getByTestId('composer-submit').click();
    const original = await firstAcceptance;
    expect(original.duplicate).toBe(false);
    const row = page.locator(`[data-message-id="${original.client_message_id}"]`);
    await expect.poll(async () => (await sessionStreamBodies(page)).flatMap((body) => sseFrames(body.text)), {
      message: 'the browser must receive the real actionable refusal before the receipt is lost',
    }).toContainEqual(expect.objectContaining({
      type: 'data-input-failed',
      data: { input_id: original.input_id, ...expectedFailure },
    }));
    await refusalHandled;
    await expect(row.getByText(SENDING)).toBeVisible();
    const stored = sessionInputRows(sessionId).find((input) => input.input_id === original.input_id)!;
    expect(stored.state).toBe('failed');
    expect(stored.admitted_at, 'preparation refused the input before native admission').toBeNull();
    expect(stored.failure, 'the fixture refusal is durably recorded').toEqual(expectedFailure);
    const cleared = await request.post(apiPath(`/sessions/${sessionId}/user-action`), {
      data: { action_id: 'authorize' },
    });
    expect(cleared.ok()).toBe(true);
    expect((await cleared.json()).data).toEqual({ ready: true, user_action: null, actions: [] });
    const lost = page.waitForEvent('requestfailed', (outbound) => (
      new URL(outbound.url()).pathname === path && outbound.method() === 'POST'
    ));
    loseReceipt();
    await lost;
    const retry = row.getByRole('button', { name: RETRY });
    // Keep the receipt lost through transport recovery: Retry means the send
    // has failed and cleared its early failure cache, rather than recovered it.
    await expect(retry).toBeVisible();
    const attemptsBeforeRetry = scene.attempts;
    retryReleased = true;
    const retried = page.waitForResponse((response) => (
      new URL(response.url()).pathname === path && response.request().method() === 'POST'
    ));
    await retry.click();
    const response = await retried;
    expect(response.status()).toBe(200);
    expect(response.request().postDataJSON()).toEqual({ content: prompt, client_message_id: original.client_message_id });
    const receipt = (await response.json()).data;
    expect(receipt).toEqual({ ...original, state: 'failed', duplicate: true, failure: expectedFailure });
    const failure = row.getByTestId('user-message-failure');
    await expect(failure).toBeVisible();
    await expect(failure).toContainText(expectedFailure.message);
    await expect(failure.locator(`a[href="${expectedFailure.user_action.url}"]`)).toBeVisible();
    await expect(failure.getByRole('button', { name: /^(Send again|重新发送)$/ })).toBeEnabled();
    expect(sessionInputRows(sessionId).map((input) => input.input_id)).toEqual([original.input_id]);
    expect(responseFor(sessionId, original.input_id), 'the refused input is never executed on retry').toBeNull();
    expect(JSON.stringify(nativeStoreRows(sessionId)), 'the supplier never received the refused prompt').not.toContain(prompt);
    const history = await api.getMessages(sessionId, 50);
    expect(history.messages.map((message) => ({ id: message.id, failure: message.metadata?.failure })))
      .toEqual([{ id: original.client_message_id, failure: expectedFailure }]);
    expect(scene.attempts, 'only the manual retry follows the lost-receipt attempts').toBe(attemptsBeforeRetry + 1);
    await test.info().attach('lost-refusal-receipt', {
      body: JSON.stringify({ original, stored, receipt, browser_stream: await sessionStreamBodies(page) }),
      contentType: 'application/json',
    });
  } finally {
    loseReceipt();
    void firstAcceptance.catch(() => undefined);
    void refusalHandled?.catch(() => undefined);
    control(false);
    await page.unroute(routePattern);
  }
});

/** The input receipt, or a loud failure naming what the response carried. */
async function acceptedReceipt(response: Response, content: string): Promise<Receipt> {
  expect(response.status(), 'the input POST should be accepted').toBe(200);
  const body = await response.json() as { data?: Record<string, unknown> };
  const receipt = body.data ?? {};
  const requestBody = response.request().postDataJSON() as { client_message_id?: string; content?: unknown };
  expect(requestBody.content, 'the console must send the exact user content').toBe(content);
  const clientMessageId = String(receipt.client_message_id || '').trim();
  const inputId = String(receipt.input_id || '').trim();
  expect(clientMessageId, 'the receipt must keep the client message id the console sent')
    .toBe(String(requestBody.client_message_id || '').trim());
  expect(inputId, 'the receipt must name the input the session recorded').toMatch(/^[0-9a-f-]{36}$/);
  expect(receipt.duplicate, 'the session must record this message as a new input').toBe(false);
  return { clientMessageId, inputId };
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
 * Wait for the first prompt's Bash tool to be running. A response that ends
 * without reaching it is a failure of this case's setup, not a reason to ask
 * again: the case sends one prompt and holds one Bash.
 */
async function waitForHeldTool(api: AstraApi, sessionId: string, startedPath: string): Promise<void> {
  const deadline = Date.now() + TOOL_START_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (await fileExists(api, sessionId, startedPath)) return;
    const detail = await api.getSession(sessionId);
    if (String(detail.state || '') === 'READY' && detail.last_response) {
      const history = visibleMessages(await api.getMessages(sessionId, 50));
      const last = history.filter((message) => message.role === 'assistant').map(messageText).at(-1) || '';
      throw new Error(
        'the source response Bash tool must pause before boundary submissions; the response ended '
        + `${String(detail.last_response.outcome)} without it (last assistant text=${JSON.stringify(last)})`,
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
  throw new Error(`user message row ${rowId} was never observed entering the DOM`);
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
  scope: string;
  entry_seq: number;
  entry_uuid: string;
  parent_uuid: string;
  assistant_descendants: Array<{ entry_seq: number; entry_uuid: string }>;
}

/**
 * The native store row holding one exact root prompt, with its linked
 * assistant successors in the same transcript. Claude's parentUuid chain
 * includes attachment/system/progress entries, not just visible messages. No
 * durable record relates the supplier's entry uuid to the platform's input
 * id, so the row is located by the exact prompt the session accepted.
 */
function nativeInputEvidence(rows: NativeStoreRow[], content: string): { exact: number; evidence: NativeInputEvidence | null } {
  const exact = rows.filter(({ entry }) => rootUserText(entry) === content);
  if (exact.length === 0) return { exact: 0, evidence: null };
  // Evidence is built from the first match even when there are several, so a
  // duplicated prompt is reported with what was stored, not as an absence.
  const [{ scope, seq, entry }] = exact;
  const uuid = String(entry.uuid || '').trim();
  expect(uuid, 'the exact native user entry must have its vendor uuid').not.toEqual('');
  const linked = new Set([uuid]);
  const assistants: NativeInputEvidence['assistant_descendants'] = [];
  for (const candidate of rows) {
    if (candidate.scope !== scope || candidate.seq <= seq) continue;
    if (!isParentLane(candidate.entry) || candidate.entry.type === 'user') continue;
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
      scope,
      entry_seq: seq,
      entry_uuid: uuid,
      parent_uuid: String(entry.parentUuid || ''),
      assistant_descendants: assistants,
    },
  };
}

interface DurableEvidence {
  client_message_id: string;
  input_id: string;
  response_id: string;
  response_outcome: string | null;
  result_sequences: number[];
  native: NativeInputEvidence;
  history_user_index: number;
  history_assistant_indices: number[];
}

interface DurableObservation {
  inputs: SessionInputRow[];
  response: SessionResponseRow | null;
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
  const inputs = sessionInputRows(sessionId).filter((row) => row.client_message_id === submission.clientMessageId);
  const response = responseFor(sessionId, submission.inputId);
  const resultSequences = response ? resultParts(sessionId, response.response_id).map((row) => row.seq) : [];
  const native = nativeInputEvidence(nativeStoreRows(sessionId), content);
  const history = (await api.getMessages(sessionId, 50)).messages;
  const historyUserIndices = history.flatMap((message, index) => (
    message.role === 'user'
    && message.id === submission.clientMessageId
    && messageText(message).trim() === content.trim()
      ? [index]
      : []
  ));
  const historyUserIndex = historyUserIndices[0] ?? -1;
  const historyAssistantIndices = history.flatMap((message, index) => (
    historyUserIndex >= 0 && index > historyUserIndex && answersInput(message, submission.inputId) ? [index] : []
  ));
  return { inputs, response, resultSequences, native, historyUserIndices, historyAssistantIndices };
}

/**
 * Wait until the retried input has reached every durable record, then judge
 * each one on its own: the session's input row, the response that answers it
 * ending with the supplier's Result, the native store holding its exact
 * prompt once with an assistant successor, and public history showing one
 * user message followed by its answer. The wait is only for arrival;
 * exactness is asserted afterwards so a duplicate or a diverging content fails
 * on its own diff instead of as a timeout.
 */
async function waitForBoundaryInputDurable(
  api: AstraApi,
  sessionId: string,
  submission: Receipt,
  content: string,
  timeoutMs: number,
): Promise<DurableEvidence> {
  const deadline = Date.now() + timeoutMs;
  let seen = await observeDurable(api, sessionId, submission, content);
  const arrived = (observation: DurableObservation) => (
    observation.inputs.length > 0
    && observation.response?.status === 'ended'
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
      'the retried input did not reach its input, response, native and history records; last='
      + JSON.stringify({
        inputs: seen.inputs,
        response: seen.response,
        result_sequences: seen.resultSequences,
        native_exact_users: seen.native.exact,
        native_assistant_descendants: seen.native.evidence?.assistant_descendants.length ?? 0,
        history_user_indices: seen.historyUserIndices,
        history_assistant_indices: seen.historyAssistantIndices,
      }),
    );
  }

  expect(seen.inputs, 'the session must record exactly one input for the retried client id').toHaveLength(1);
  const [input] = seen.inputs;
  expect(input.input_id, 'the input row must be the one the receipt named').toBe(submission.inputId);
  expect(input.content, 'the input row must carry the exact prompt').toBe(content);
  expect(input.state, 'the runtime must have taken the input').toBe('admitted');
  expect(input.response_id, 'the input row must name the response that answered it').toBe(seen.response!.response_id);
  expect(seen.response!.outcome, 'the answer must complete').toBe('completed');
  expect(seen.resultSequences, 'the answer must end with one supplier Result').toHaveLength(1);
  expect(seen.native.exact, 'the native store must hold the exact prompt once').toBe(1);
  expect(seen.historyUserIndices, 'public history must show the input once').toHaveLength(1);
  expect(seen.historyAssistantIndices, 'public history must show one answer to the input').toHaveLength(1);
  return {
    client_message_id: submission.clientMessageId,
    input_id: submission.inputId,
    response_id: seen.response!.response_id,
    response_outcome: seen.response!.outcome,
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
  const inputsPath = apiPath(`/sessions/${sessionId}/inputs`);
  const routePattern = `**${inputsPath}`;
  const isInputPost = (response: Response) => (
    response.url().includes(inputsPath) && response.request().method() === 'POST'
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
    const source = await acceptedReceipt(await sourceDelivery.response, sourcePrompt);
    expect(source.clientMessageId).toBe(sourceDelivery.clientMessageId);
    await waitForHeldTool(api, sessionId, toolStartedPath);

    // Both submissions must originate while a response runs.
    await expect(statusPill, 'both submissions must originate while a response runs')
      .toHaveAttribute('data-state', 'PROCESSING', { timeout: 30_000 });
    // A running response keeps its tool cards inside a process group that
    // starts closed, so the card is readable only once the reader opens it.
    await openLiveProcessGroup(page);
    await expect(
      page.getByRole('button', { name: /Bash/ }).first(),
      'the held command reads as working while it waits on the release file',
    ).toHaveText(/Working|处理中/, { timeout: 30_000 });

    // The response answering the source input, from the platform's records
    // rather than from an identity formula.
    await expect.poll(() => responseFor(sessionId, source.inputId)?.response_id ?? '', {
      timeout: 30_000,
      intervals: [500, 1_000],
      message: 'the platform must record the response answering the source input',
    }).not.toBe('');
    const sourceResponse = responseFor(sessionId, source.inputId)!;
    const sourceResponseId = sourceResponse.response_id;
    expect(sourceResponse.status, 'the source response runs while its Bash is held').toBe('running');
    scene.sourceResponseId = sourceResponseId;
    await expect(
      page.locator(`[data-message-id="${source.clientMessageId}"] [data-testid="user-message"]`),
      'the source input must be on screen under its client message id',
    ).toHaveCount(1, { timeout: 30_000 });
    await expect(
      page.locator(`[data-message-id="${sourceResponseId}"] [data-testid="assistant-message"]`),
      'the running response must be on screen under its response id',
    ).toHaveCount(1, { timeout: 30_000 });
    // The page writes the response from the stream and the detail reads the
    // platform's records; they converge, so the cross-check is polled.
    await expect.poll(async () => (
      ((await api.getSession(sessionId)).current_responses ?? []).map((response) => response.response_id)
    ), {
      timeout: 15_000,
      intervals: [500, 1_000],
      message: 'the session detail must name the same running response',
    }).toEqual([sourceResponseId]);
    expect(resultParts(sessionId, sourceResponseId), 'the source Result must not exist while Bash is held').toEqual([]);
    const inputsBaseline = sessionInputRows(sessionId).map((row) => row.input_id);

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

    // The message is shown the moment it is sent, so its first paint is
    // watched from before the send.
    await installInsertionObserver(page);
    const composer = page.getByTestId('composer-prompt');
    await expect(composer, 'a busy conversation still accepts the boundary input').toBeEnabled({ timeout: 30_000 });
    await composer.fill(boundaryPrompt);
    const failedResponsePromise = page.waitForResponse(isInputPost, { timeout: 45_000 });
    await page.getByTestId('composer-submit').click();
    const failedResponse = await failedResponsePromise;
    expect(failedResponse.status(), 'the first boundary input is intentionally failed').toBe(503);
    const failedRequest = failedResponse.request().postDataJSON() as {
      client_message_id?: string;
      content?: unknown;
    };
    const failedClientMessageId = String(failedRequest.client_message_id || '').trim();
    expect(failedRequest.content).toBe(boundaryPrompt);
    expect(failedClientMessageId, 'the retryable local failure must retain its client id').not.toEqual('');
    const boundaryRowId = failedClientMessageId;
    const boundaryRow = page.locator(`[data-message-id="${boundaryRowId}"]`);
    const failureNote = boundaryRow.getByTestId('user-message-failure');
    await expect(
      failureNote,
      'a message the session never received must stay on screen marked as not sent',
    ).toBeVisible({ timeout: 30_000 });
    await expect(failureNote, 'a refused message must show its exact error').toContainText(FORCED_FAILURE_TEXT);
    await expect(boundaryRow.getByTestId('user-message')).toContainText(boundaryMarker);
    await expect(
      page.getByTestId('user-message').filter({ hasText: boundaryMarker }),
      'the refused message is on screen once',
    ).toHaveCount(1);
    await expect(
      failureNote.getByRole('button', { name: RETRY }),
      'a message the session never received must remain retryable',
    ).toBeEnabled();
    expect(
      sessionInputRows(sessionId).filter((row) => row.client_message_id === failedClientMessageId),
      'the browser-injected failure must leave no input on the platform',
    ).toEqual([]);
    expect(
      sessionInputRows(sessionId).map((row) => row.input_id),
      'nothing may be recorded for a refused input',
    ).toEqual(inputsBaseline);
    expect(
      nativeInputEvidence(nativeStoreRows(sessionId), boundaryPrompt).exact,
      'the engine must not have received a refused prompt',
    ).toBe(0);
    expect(
      ((await api.getSession(sessionId)).held_inputs ?? []).map((input) => String(input.client_message_id || '')),
      'the platform must not hold an input it never accepted',
    ).not.toContain(failedClientMessageId);

    const insertion = await waitForInsertion(page, boundaryRowId, 30_000);
    expect(insertion.index, 'the next prompt must never first-paint at the top').toBeGreaterThan(0);
    expect(
      insertion.rows[insertion.index - 1],
      'a message sent while a response runs must first-paint immediately after that response',
    ).toMatchObject({ id: sourceResponseId, kind: 'assistant' });

    // 3. Retry, and hold the retry before acceptance.
    retryResponsePromise = page.waitForResponse(isInputPost, { timeout: TURN_TIMEOUT_MS });
    await failureNote.getByRole('button', { name: RETRY }).click();
    const heldAt = Date.now();
    await expect.poll(() => heldRoute !== null, {
      timeout: 45_000,
      intervals: [100, 250],
      message: 'the retry must be held at the inputs route',
    }).toBe(true);
    const heldRequest = heldRoute!.request().postDataJSON() as { client_message_id?: string; content?: unknown };
    expect(String(heldRequest.client_message_id || '').trim(), 'retry must resend the same client message id').toBe(failedClientMessageId);
    expect(heldRequest.content, 'retry must resend the same content').toBe(boundaryPrompt);
    await expect(
      boundaryRow.getByRole('status'),
      'a retried message reads as on its way while it is held',
    ).toHaveText(SENDING, { timeout: 30_000 });
    await expect(failureNote, 'a message on its way is not marked as failed').toHaveCount(0);
    await expect(statusPill, 'the header stays live while the retry is held').toHaveAttribute('data-state', 'PROCESSING');
    await expect(
      page.getByTestId('user-message').filter({ hasText: boundaryMarker }),
      'retrying keeps the one message on screen rather than adding another',
    ).toHaveCount(1);
    expect(
      sessionInputRows(sessionId).filter((row) => row.client_message_id === failedClientMessageId),
      'a held request has reached no backend',
    ).toEqual([]);
    expect(
      ((await api.getSession(sessionId)).current_responses ?? []).map((response) => response.response_id),
      'the source response must still be the running one while the retry is held',
    ).toEqual([sourceResponseId]);

    // 4. Release the real Bash; require the supplier's actual Result for the
    // source input, and the source response still on screen, before the held
    // retry reaches the backend. A ready session is not that evidence. The
    // terminal's own output echoes the command, so the release file is proved
    // through the file API the started marker already used.
    await api.runTerminalCommand(sessionId, `touch ${toolReleasePath}`, '/tmp', 30_000);
    expect(
      await fileExists(api, sessionId, toolReleasePath),
      'the release file must exist after the terminal wrote it — the blocking tool waits on this exact path',
    ).toBe(true);
    await expect.poll(() => resultParts(sessionId, sourceResponseId).length, {
      timeout: 90_000,
      intervals: [500, 1_000],
      message: 'the source response must carry the supplier Result before the held input is accepted',
    }).toBeGreaterThan(0);
    await expect.poll(() => responseRow(sessionId, sourceResponseId)?.status ?? '', {
      timeout: 30_000,
      intervals: [500, 1_000],
      message: 'the source response must have ended before the held input is accepted',
    }).toBe('ended');
    const sourceEnded = responseRow(sessionId, sourceResponseId)!;
    expect(sourceEnded.outcome, 'the source response completes once its Bash is released').toBe('completed');
    const sourceResultSequences = resultParts(sessionId, sourceResponseId).map((row) => row.seq);
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

    // 5. The receipt keeps the identity the failed attempt had.
    const submission = await acceptedReceipt(retryResponse, boundaryPrompt);
    expect(submission.clientMessageId, 'retry must preserve the original client message identity')
      .toBe(failedClientMessageId);

    // 6. One user row, its status line gone, and its answer directly after it.
    // The virtualized list wraps each message container in its own index
    // node, so adjacency is read from the rendered row order rather than a
    // CSS sibling selector; the answer container is then located by the exact
    // response id the platform recorded for this input.
    await expect(boundaryRow).toHaveCount(1, { timeout: 60_000 });
    await expect.poll(() => responseFor(sessionId, submission.inputId)?.response_id ?? '', {
      timeout: TURN_TIMEOUT_MS,
      intervals: [500, 1_000],
      message: 'the platform must record the response answering the retried input',
    }).not.toBe('');
    const boundaryResponseId = responseFor(sessionId, submission.inputId)!.response_id;
    scene.boundaryResponseId = boundaryResponseId;
    await expect(
      boundaryRow.getByRole('status'),
      'the message stops reading as sending or queued once the runtime has it',
    ).toHaveCount(0, { timeout: 60_000 });
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
        message: 'the next prompt response must render its answer',
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
    expect(durable.response_id, 'the recorded answer must be the response the page showed').toBe(boundaryResponseId);
    const settled = await api.waitForSession(
      sessionId,
      (detail) => (
        detail.state === 'READY'
        && (detail.current_responses ?? []).length === 0
        && (detail.held_inputs ?? []).length === 0
      ),
      TURN_TIMEOUT_MS,
    );
    expect(settled.last_error, 'the boundary prompt must settle without an error').toBeFalsy();
    expect(settled.last_response?.response_id, 'the last response is the answer to the retried input').toBe(boundaryResponseId);
    expect(settled.last_response?.outcome).toBe('completed');
    await expect(statusPill, 'the accepted second input must not remain permanently sending')
      .toHaveText(/Ready|就绪/, { timeout: 60_000 });
    await expect(page.getByTestId('user-message-failure')).toHaveCount(0);
    expect(scene.attempts, 'one refused attempt and one manual retry reach the route').toBe(2);

    // 7. The complete reloaded timeline equals the live one: every row's
    // identity and role, in order, through the same virtualized surface. The
    // live shape is read here, immediately before the reload, not from the
    // earlier order check: rows settle between the two.
    const liveShape = (await readTimelineRows(page)).map((row) => ({ id: row.id, kind: row.kind }));
    expect(
      liveShape.some((row) => row.kind === 'user' && row.id === boundaryRowId),
      'live timeline must contain the exact retried input row before reload',
    ).toBe(true);
    const publicRoles = (await api.getMessages(sessionId, 50)).messages.map((message) => message.role);
    expect(
      liveShape.filter((row) => row.kind !== 'other').map((row) => row.kind),
      'the live timeline must show every public row in public order',
    ).toEqual(publicRoles);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(
      page.locator(`[data-message-id="${boundaryRowId}"] [data-testid="user-message"]`),
      'reloading must restore the exact retried input row',
    ).toHaveCount(1, { timeout: 60_000 });
    await expect.poll(
      async () => (await readTimelineRows(page)).map((row) => ({ id: row.id, kind: row.kind })),
      {
        timeout: 60_000,
        intervals: [250, 500, 1_000],
        message: 'reloaded timeline row ids and order must equal the live first-paint timeline',
      },
    ).toEqual(liveShape);
    await expect(page.getByTestId('user-message-failure')).toHaveCount(0);
    await expect(page.getByTestId('composer-prompt')).toBeEnabled({ timeout: 30_000 });

    test.info().annotations.push({
      type: 'e2e_native_input_result_boundary',
      description: JSON.stringify({
        session_id: sessionId,
        source: { ...source, response_id: sourceResponseId, result_sequences: sourceResultSequences },
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
    // An aborted hold rejects a response wait that nothing awaits.
    void retryResponsePromise?.catch(() => undefined);
    await page.unroute(routePattern).catch(() => undefined);
  }
});
