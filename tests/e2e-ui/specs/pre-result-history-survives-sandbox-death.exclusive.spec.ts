/**
 * User history flushed before the SDK Result must survive loss of the sandbox
 * that was still running the tool. This pins the eager SessionStore boundary:
 * the box takes its response with it (ended `failed`, `sandbox_lost`, never
 * replayed), then the cold page reloads and the next message rebuilds on a
 * fresh box from the restored SessionStore.
 */
import { expect, test } from '@playwright/test';

import { AstraApi, messageText, type InputReceipt } from '../fixtures/astraApi';
import {
  postgresRows,
  postgresTextRows,
  sessionInputRows,
  sessionInteractionRows,
  sessionOutputRows,
  sessionResponseRows,
  sqlLiteral,
  type SessionResponseRow,
} from '../fixtures/dbOracle';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { browserStreamFrames, object } from '../fixtures/nativeMcpServer';
import {
  killSandbox,
  requireSandboxHandle,
  sandboxExec,
  sandboxRunning,
  waitForSandboxStopped,
} from '../fixtures/sandboxOps';
import { POSTGRES_CONTAINER_HANDLE, requireServiceContainer } from '../fixtures/serviceContainer';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { mirrorSseBodies } from '../fixtures/sseBodies';

const TURN_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_REBORROW_TURN_TIMEOUT_MS', 240_000);
const KILL_CONVERGE_MS = parseTimeoutEnv('ASTRABOX_E2E_KILL_CONVERGE_MS', 30_000);

/** One line of the conversation's main native transcript, as the runtime wrote it. */
interface NativeRootRow {
  seq: number;
  uuid: string;
  entry_json: string;
  entry: Record<string, unknown>;
}

let agentId = '';
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

/** The main (non-sub-agent) native transcript the platform holds, in the runtime's order. */
function nativeRootRows(sessionId: string): NativeRootRow[] {
  return postgresRows<{ seq: number; entry_json: string }>(
    `SELECT e.seq, e.entry_json FROM transcript_entries e `
      + `JOIN transcript_scopes s ON s.scope_id = e.scope_id `
      + `WHERE s.platform_session_id = ${sqlLiteral(sessionId)} AND s.subpath = '' `
      + `ORDER BY s.scope_id, e.seq`,
  ).map((row) => {
    const entry = object(JSON.parse(String(row.entry_json)));
    return { seq: Number(row.seq), uuid: String(entry.uuid || ''), entry_json: String(row.entry_json), entry };
  });
}

function userEntriesWithMarker(sessionId: string, marker: string): NativeRootRow[] {
  return nativeRootRows(sessionId).filter(
    (row) => String(row.entry.type || '') === 'user' && JSON.stringify(row.entry).includes(marker),
  );
}

/** The responses that answer `inputId`, as the session's reader recorded them. */
function responsesTo(sessionId: string, inputId: string): SessionResponseRow[] {
  return sessionResponseRows(sessionId).filter((row) => row.input_ids.includes(inputId));
}

/** How one response ended, or that it has not. */
function responseEnd(row: SessionResponseRow | undefined) {
  return row
    ? { status: row.status, outcome: row.outcome, native_reason: row.native_reason }
    : null;
}

/** Whether one response has ended, and how; the runtime's own reason is not compared. */
function responseOutcome(row: SessionResponseRow | undefined) {
  return row ? { status: row.status, outcome: row.outcome } : null;
}

/** The session row's lifecycle and box binding. */
interface SessionBindingRow {
  state: string;
  sandbox_id: string | null;
  runtime_unavailable: boolean;
  last_error: string | null;
}

function sessionBinding(sessionId: string): SessionBindingRow | null {
  return postgresRows<SessionBindingRow>(
    `SELECT state, sandbox_id, runtime_unavailable, last_error FROM sessions `
      + `WHERE session_id = ${sqlLiteral(sessionId)}`,
  )[0] ?? null;
}

/**
 * Move the session's recorded lease into the past while it still names `sandboxId`.
 *
 * Compare-and-set on the box, so a convergence that already cleared the binding
 * is not overwritten; the statement returns what it wrote (session, box and the
 * stored expiry in epoch milliseconds).
 */
function lapseSessionLease(sessionId: string, sandboxId: string, expiredAt: string): string[][] {
  return postgresTextRows(
    requireServiceContainer(POSTGRES_CONTAINER_HANDLE),
    process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox',
    process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox',
    `UPDATE sessions SET expires_at = ${sqlLiteral(expiredAt)}::timestamptz `
      + `WHERE session_id = ${sqlLiteral(sessionId)} AND sandbox_id = ${sqlLiteral(sandboxId)} `
      + `RETURNING session_id, sandbox_id, (extract(epoch FROM expires_at) * 1000)::bigint`,
  ).map((line) => line.split('|'));
}

test('pre-Result user history survives sandbox death and the next-turn rebuild', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = `${Date.now()}-${test.info().workerIndex}`;
  const userMarker = `E2E_PRE_RESULT_HISTORY_${runId}`;
  const replyMarker = `E2E_POST_DEATH_REPLY_${runId}`;
  const agent = await api.createColdTestAgent(`__e2e_pre_result_${runId}`);
  agentId = agent.agent_id;
  const created = await api.startConversation(agentId);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  const ready = await api.waitForSessionReady(sessionId);
  await api.setPermissionMode(sessionId, 'bypassPermissions');
  const oldSandboxId = String(ready.sandbox_id || '').trim();
  expect(oldSandboxId, 'the running response must have a sandbox to lose').not.toEqual('');
  const workspace = String(ready.terminal_cwd || '').trim();
  expect(workspace, 'a READY session must publish its workspace').toMatch(/^\/.+/);
  // The fixture and the agent share this box's workspace, not their private /tmp.
  const startedPath = `${workspace}/astrabox-pre-result-${runId}.started`;
  const holdPath = `${workspace}/astrabox-pre-result-${runId}.hold`;
  const oldSandbox = await requireSandboxHandle(api, oldSandboxId);
  sandboxExec(oldSandbox, `touch ${holdPath}`);
  expect(sandboxExec(oldSandbox, `test -f ${holdPath} && echo yes`).trim()).toBe('yes');

  // Only the old box receives permission to block. Were the input ever handed
  // to a volume-free replacement again, it must not find that permission.
  const prompt = [
    userMarker,
    'Use the Bash tool exactly once to run this command verbatim:',
    '```bash',
    `if [ -f ${holdPath} ]; then touch ${startedPath}; while [ -f ${holdPath} ]; do sleep 0.2; done; fi; echo released`,
    '```',
    'Do not use another tool. Wait for Bash before replying.',
  ].join('\n');
  // The response is read off the session's stream from the position before the
  // send, so the read covers everything the response writes, its end included.
  const afterSeq = Number((await api.getSession(sessionId)).output_seq ?? 0);
  const receipt: InputReceipt = await api.postTurnInput(sessionId, prompt);
  const streaming = api.readResponse(sessionId, afterSeq, receipt.input_id, Date.now() + TURN_BUDGET_MS)
    .then((read) => ({ ...read, error: '' }))
    .catch((error: unknown) => ({
      frames: [] as Record<string, unknown>[],
      raw: '',
      error: String((error as Error)?.message ?? error),
    }));

  await expect.poll(
    () => sandboxExec(oldSandbox, `test -f ${startedPath} && echo yes || true`).trim(),
    {
      timeout: 150_000,
      intervals: [1_000, 2_000],
      message: 'the model must enter the blocking Bash call before compute loss',
    },
  ).toBe('yes');
  await expect.poll(
    () => userEntriesWithMarker(sessionId, userMarker).length,
    {
      timeout: 60_000,
      intervals: [500, 1_000, 2_000],
      message: 'the pre-Result user entry must reach the transcript repository',
    },
  ).toBe(1);
  const originalUserEntry = userEntriesWithMarker(sessionId, userMarker)[0];
  expect(originalUserEntry.uuid, 'the pre-Result record must have its native identity').toBeTruthy();
  // The tool runs in the box while the reader records what it read, so the
  // records are awaited rather than sampled.
  await expect.poll(() => ({
    responses: responsesTo(sessionId, receipt.input_id).map((row) => row.status),
    input: sessionInputRows(sessionId).find((row) => row.input_id === receipt.input_id)?.state ?? null,
  }), {
    timeout: 15_000,
    message: 'the blocked tool must belong to the one running response answering a message the runtime took',
  }).toEqual({ responses: ['running'], input: 'admitted' });
  const originalResponseId = responsesTo(sessionId, receipt.input_id)[0].response_id;

  killSandbox(oldSandbox);
  await waitForSandboxStopped(oldSandbox, KILL_CONVERGE_MS);
  // The response the box took with it ends failed, recorded once and never re-run.
  await expect.poll(() => responseEnd(responsesTo(sessionId, receipt.input_id)[0]), {
    timeout: TURN_BUDGET_MS,
    intervals: [500, 1_000, 2_000],
    message: 'the lost response must end failed with its box',
  }).toEqual({ status: 'ended', outcome: 'failed', native_reason: 'sandbox_lost' });
  const streamed = await streaming;
  expect(streamed.error, 'the stream must deliver the lost response to its end').toEqual('');
  const streamedTypes = streamed.frames.map((frame) => String(frame.type ?? ''));
  expect(streamedTypes, 'the console is told the response failed').toContain('error');
  expect(streamedTypes.at(-1), 'the platform closes the lost response').toBe('finish');
  await api.waitForSession(sessionId, (session) => session.state === 'READY', TURN_BUDGET_MS);

  expect(
    userEntriesWithMarker(sessionId, userMarker),
    'the repository must retain exactly one pre-Result user entry after box loss',
  ).toHaveLength(1);
  await page.goto(appPath(`/sessions/${sessionId}`));
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
  await expect(
    page.getByTestId('user-message').filter({ hasText: userMarker }),
    'cold history must render the pre-Result user message exactly once',
  ).toHaveCount(1);

  const nextPrompt = `Do not use tools. Reply with exactly ${replyMarker}.`;
  const result = await api.sendTurn(
    sessionId,
    nextPrompt,
    TURN_BUDGET_MS,
  );
  expect(result.errorText, 'the next message must rebuild without an error frame').toBeNull();
  // The stream can deliver the end before the reader has recorded it.
  await expect.poll(() => responseOutcome(responsesTo(sessionId, result.inputId)[0]), {
    timeout: TURN_BUDGET_MS,
    message: 'the next message must be answered and completed on the replacement box',
  }).toEqual({ status: 'ended', outcome: 'completed' });
  await expect.poll(async () => {
    const history = await api.getMessages(sessionId, 100);
    return history.messages.filter(
      (message) => message.role === 'assistant' && messageText(message).includes(replyMarker),
    ).length;
  }, {
    timeout: TURN_BUDGET_MS,
    message: 'the rebuilt response must persist one exact reply',
  }).toBe(1);
  expect(
    responsesTo(sessionId, receipt.input_id).map((row) => row.response_id),
    'the lost response is never replayed: the first message keeps its one failed response',
  ).toEqual([originalResponseId]);
  const rebuilt = await api.waitForSessionReady(sessionId, TURN_BUDGET_MS);
  const rebuiltSandboxId = String(rebuilt.sandbox_id || '').trim();
  expect(rebuiltSandboxId).not.toEqual(oldSandboxId);
  // Native history survives; this old-box-only fixture deliberately does not.
  expect(
    sandboxExec(
      await requireSandboxHandle(api, rebuiltSandboxId),
      `test ! -e ${holdPath} && test ! -e ${startedPath} && echo yes || true`,
    ).trim(),
    'native recovery must not depend on the old box retaining fixture files',
  ).toBe('yes');
  // SDK execution attempts are not distinct platform user inputs. Preserve the
  // original opaque record, and check input uniqueness on the product history.
  const retainedOriginal = userEntriesWithMarker(sessionId, userMarker)
    .filter((entry) => entry.uuid === originalUserEntry.uuid);
  expect(retainedOriginal, 'the original native record must survive exactly once').toHaveLength(1);
  expect(JSON.parse(retainedOriginal[0].entry_json)).toEqual(JSON.parse(originalUserEntry.entry_json));
  const history = await api.getMessages(sessionId, 100);
  expect(history.has_more).toBe(false);
  expect(history.messages.filter((message) => message.role === 'user').map(messageText))
    .toEqual([prompt, nextPrompt]);
  await page.reload();
  await expect(page.getByTestId('user-message').filter({ hasText: userMarker })).toHaveCount(1);
  await expect(page.getByTestId('user-message').filter({ hasText: nextPrompt })).toHaveCount(1);
});

test('pending Write pre-Result history survives background sandbox death convergence and the next send', async ({
  page,
  request,
}) => {
  const api = new AstraApi(request);
  const runId = `${Date.now()}-${test.info().workerIndex}`;
  const historyMarker = `E2E_PENDING_WRITE_HISTORY_${runId}`;
  const replyMarker = `E2E_PENDING_WRITE_REBUILT_${runId}`;
  const fileName = `e2e-expired-pending-${runId}.txt`;
  const filePath = `/tmp/${fileName}`;
  const prompt = [
    `E2E sandbox expiry while waiting ${historyMarker}.`,
    `Call Write directly with file_path="${filePath}" and content="expired pending".`,
    'The absolute path is supplied; do not look up the working directory or inspect files first.',
    'Do not use another tool and wait for permission before continuing.',
  ].join('\n');
  const agent = await api.createColdTestAgent(`__e2e_pending_write_death_${runId}`);
  agentId = agent.agent_id;
  const created = await api.startConversation(agentId);
  const sessionId = created.session_id;
  sessions.push(sessionId);
  const ready = await api.waitForSessionReady(sessionId);
  await api.setPermissionMode(sessionId, 'default');
  const oldSandboxId = String(ready.sandbox_id || '').trim();
  expect(oldSandboxId).not.toBe('');
  const oldSandbox = await requireSandboxHandle(api, oldSandboxId);
  expect(sandboxRunning(oldSandbox)).toBe(true);
  const nativeUsers = () => nativeRootRows(sessionId).filter((row) => row.entry.type === 'user'
    && JSON.stringify(row.entry).includes(historyMarker));

  await mirrorSseBodies(page);
  await openSessionView(page, sessionId);
  const firstResponse = await sendPrompt(page, sessionId, prompt);
  const firstReceipt = ((await firstResponse.json()) as { data: InputReceipt }).data;
  const pending = await api.waitForPendingInteraction(sessionId, TURN_BUDGET_MS);
  expect(pending.presentation).toBe('tool_approval');
  expect(pending.tool_name).toBe('Write');
  const responseId = String(pending.response_id || '').trim();
  const interactionId = String(pending.interaction_id || '').trim();
  const toolId = String(pending.tool_call_id || '').trim();
  for (const id of [responseId, interactionId, toolId]) expect(id).not.toBe('');
  expect(String(object(pending.raw_input).file_path)).toBe(filePath);
  await expect(page.getByTestId('pending-interaction-panel')).toBeVisible();
  await expect(page.getByTestId('pending-interaction-panel')).toContainText('Write');
  await expect(page.getByTestId('session-conversation-shell')).toHaveAttribute('data-pending-tool-call-id', toolId);
  const firstUser = page.getByTestId('user-message').filter({ hasText: historyMarker });
  await expect(firstUser).toHaveCount(1);
  let renderedFirstUser = '';
  await expect.poll(async () => {
    renderedFirstUser = await firstUser.innerText();
    return renderedFirstUser.trim();
  }, { message: 'the original input must render beyond the hidden measurement pass before capture' }).not.toBe('');
  const interactionRow = () => sessionInteractionRows(sessionId)
    .find((row) => row.interaction_id === interactionId);
  await expect.poll(() => ({
    response: responseEnd(responsesTo(sessionId, firstReceipt.input_id)[0]),
    response_id: responsesTo(sessionId, firstReceipt.input_id)[0]?.response_id ?? null,
    interaction: interactionRow() ? { status: interactionRow()!.status, response_id: interactionRow()!.response_id } : null,
  }), { message: 'the response answering the message must be recorded waiting on the open approval' }).toEqual({
    response: { status: 'waiting', outcome: null, native_reason: null },
    response_id: responseId,
    interaction: { status: 'open', response_id: responseId },
  });
  await expect.poll(() => nativeUsers().length, {
    message: 'the real pending Write input must reach database custody before any SDK Result',
  }).toBe(1);
  const nativeToolCalls = () => nativeRootRows(sessionId).flatMap((row) => {
    if (row.entry.type !== 'assistant') return [];
    const content = object(row.entry.message).content;
    return Array.isArray(content) ? content.map(object).filter((block) => block.type === 'tool_use'
      && block.id === toolId && block.name === 'Write') : [];
  });
  await expect.poll(() => nativeToolCalls().length).toBe(1);
  expect(String(object(nativeToolCalls()[0].input).file_path)).toBe(filePath);
  const nativeBefore = nativeRootRows(sessionId);
  const originalInputs = sessionInputRows(sessionId);
  expect(originalInputs.map((row) => ({ input_id: row.input_id, content: row.content, state: row.state })))
    .toEqual([{ input_id: firstReceipt.input_id, content: prompt, state: 'admitted' }]);

  // With every browser observer gone, neither a page read nor a new send can
  // be mistaken for autonomous convergence of the dead, waiting response; the
  // readings below come from the database alone.
  await page.goto('about:blank');
  expect(sessionBinding(sessionId)?.sandbox_id).toBe(oldSandboxId);
  killSandbox(oldSandbox);
  await waitForSandboxStopped(oldSandbox, KILL_CONVERGE_MS);
  expect(sandboxRunning(oldSandbox)).toBe(false);
  // A lapsed lease makes the dead box eligible for the watcher's pull probe.
  const expiredAt = new Date(Date.now() - 60 * 60 * 1_000).toISOString();
  const lapse = lapseSessionLease(sessionId, oldSandboxId, expiredAt);
  await test.info().attach('pending-write-pre-result-fault', {
    body: JSON.stringify({ sessionId, oldSandboxId, responseId, interactionId, toolId, pending,
      firstReceipt, originalInputs, nativeBefore, expiredAt, lapse }),
    contentType: 'application/json',
  });
  // No row means a push convergence already cleared the binding.
  if (lapse.length) expect(lapse).toEqual([[sessionId, oldSandboxId, String(Date.parse(expiredAt))]]);
  let converged: SessionBindingRow | null = null;
  await expect.poll(() => {
    converged = sessionBinding(sessionId);
    return {
      state: converged?.state,
      sandbox: converged?.sandbox_id || null,
      unavailable: converged?.runtime_unavailable,
      response: responseEnd(responsesTo(sessionId, firstReceipt.input_id)[0]),
      interaction: interactionRow()?.status ?? null,
      openInteractions: sessionInteractionRows(sessionId).filter((row) => row.status === 'open').length,
    };
  }, { timeout: KILL_CONVERGE_MS, intervals: [500, 1_000],
    message: 'background lifecycle convergence must unbind dead compute, end the waiting response with its box '
      + 'and withdraw its approval without user action' })
    .toEqual({
      state: 'READY',
      sandbox: null,
      unavailable: true,
      response: { status: 'ended', outcome: 'failed', native_reason: 'sandbox_lost' },
      interaction: 'withdrawn',
      openInteractions: 0,
    });
  // Every death-convergence writer records the loss on the row.
  expect(String(converged!.last_error || '').trim(), 'the convergence must record the loss on the row').not.toEqual('');
  expect(sessionInputRows(sessionId), 'convergence must not accept or re-deliver an input').toEqual(originalInputs);
  expect(nativeRootRows(sessionId).slice(0, nativeBefore.length)).toEqual(nativeBefore);
  await test.info().attach('pending-write-background-convergence', {
    body: JSON.stringify({ sessionId, oldSandboxId, converged, interaction: interactionRow(),
      response: responsesTo(sessionId, firstReceipt.input_id)[0] }),
    contentType: 'application/json',
  });

  await openSessionView(page, sessionId);
  await expect(page.getByTestId('pending-interaction-panel')).toHaveCount(0);
  await expect(firstUser).toHaveCount(1);
  await expect(firstUser).toHaveText(renderedFirstUser, { useInnerText: true });
  const coldHistory = await api.getMessages(sessionId, 100);
  expect(coldHistory.has_more).toBe(false);
  expect(coldHistory.pending_interaction ?? null, 'history offers no approval to answer').toBeNull();
  expect(coldHistory.messages.filter((message) => message.role === 'user').map(messageText)).toEqual([prompt]);

  const nextPrompt = `Do not use tools. Reply with exactly ${replyMarker}.`;
  const nextResponse = await sendPrompt(page, sessionId, nextPrompt);
  const nextReceipt = ((await nextResponse.json()) as { data: InputReceipt }).data;
  await expect.poll(() => sessionInputRows(sessionId).filter((row) => row.input_id === nextReceipt.input_id).length)
    .toBe(1);
  await expect.poll(() => responseOutcome(responsesTo(sessionId, nextReceipt.input_id)[0]), {
    timeout: TURN_BUDGET_MS,
    message: 'the next message must be answered and completed on the replacement box',
  }).toEqual({ status: 'ended', outcome: 'completed' });
  const nextResponseId = responsesTo(sessionId, nextReceipt.input_id)[0].response_id;
  expect(nextResponseId).not.toBe(responseId);
  expect(responsesTo(sessionId, nextReceipt.input_id), 'the next message is answered once').toHaveLength(1);
  expect(
    responsesTo(sessionId, firstReceipt.input_id).map((row) => row.response_id),
    'the lost response is never replayed',
  ).toEqual([responseId]);
  await expect.poll(async () => (await api.getMessages(sessionId, 100)).messages.filter(
    (message) => message.role === 'assistant' && messageText(message).includes(replyMarker),
  ).length, { timeout: TURN_BUDGET_MS }).toBe(1);
  const rebuilt = await api.waitForSessionReady(sessionId);
  const newSandboxId = String(rebuilt.sandbox_id || '').trim();
  expect(newSandboxId).not.toBe('');
  expect(newSandboxId).not.toBe(oldSandboxId);
  expect(rebuilt.session_id).toBe(sessionId);
  expect(rebuilt.pending_interaction || null).toBeNull();
  expect(rebuilt.last_error || null).toBeNull();
  expect(rebuilt.last_response?.response_id).toBe(nextResponseId);
  expect(rebuilt.last_response?.outcome).toBe('completed');
  // What the open page's own stream carried for the next message: from the
  // `start` naming its input to its `finish`.
  const isNextStart = (frame: Record<string, unknown>) => frame.type === 'start'
    && Array.isArray((frame.messageMetadata as { input_ids?: unknown } | undefined)?.input_ids)
    && ((frame.messageMetadata as { input_ids: unknown[] }).input_ids).map(String).includes(nextReceipt.input_id);
  const nextFrames = async () => {
    const frames = await browserStreamFrames(page);
    const start = frames.findIndex(isNextStart);
    if (start < 0) return [];
    const end = frames.findIndex((frame, index) => index > start && frame.type === 'finish');
    return end < 0 ? frames.slice(start) : frames.slice(start, end + 1);
  };
  await expect.poll(async () => (await nextFrames()).filter((frame) => frame.type === 'finish').length).toBe(1);
  const frames = await nextFrames();
  expect(frames.filter((frame) => frame.type === 'error' || frame.type === 'unparsed')).toEqual([]);
  expect(
    (await browserStreamFrames(page)).filter(isNextStart),
    'the page receives the response to the next message once',
  ).toHaveLength(1);
  expect(frames.filter((frame) => frame.type === 'text-delta').map((frame) => String(frame.delta || '')).join(''))
    .toContain(replyMarker);
  expect(
    sessionOutputRows(sessionId, nextResponseId).filter((row) => row.part.type === 'error'),
    'the recorded response carries no error',
  ).toEqual([]);
  const afterInputs = sessionInputRows(sessionId);
  expect(afterInputs).toHaveLength(2);
  expect(afterInputs[0]).toEqual(originalInputs[0]);
  expect(afterInputs[1].content).toBe(nextPrompt);
  // Retain the complete ordered native prefix, including metadata without a
  // UUID. Additional supplier-owned resume records are not platform inputs.
  expect(nativeRootRows(sessionId).slice(0, nativeBefore.length)).toEqual(nativeBefore);
  const history = await api.getMessages(sessionId, 100);
  expect(history.has_more).toBe(false);
  expect(history.messages.filter((message) => message.role === 'user').map(messageText)).toEqual([prompt, nextPrompt]);
  const nextUser = page.getByTestId('user-message').filter({ hasText: nextPrompt });
  await expect(nextUser).toHaveCount(1);
  let renderedNextUser = '';
  await expect.poll(async () => {
    renderedNextUser = await nextUser.innerText();
    return renderedNextUser.trim();
  }, { message: 'the second input must render beyond the hidden measurement pass before capture' }).not.toBe('');
  await page.reload();
  await expect(page.getByTestId('run-view')).toBeVisible();
  await expect(page.getByTestId('pending-interaction-panel')).toHaveCount(0);
  await expect(firstUser).toHaveCount(1);
  await expect(nextUser).toHaveCount(1);
  await expect(firstUser).toHaveText(renderedFirstUser, { useInnerText: true });
  await expect(nextUser).toHaveText(renderedNextUser, { useInnerText: true });
  await expect(page.getByTestId('assistant-message').filter({ hasText: replyMarker })).toHaveCount(1);
  await test.info().attach('pending-write-rebuilt-history', {
    body: JSON.stringify({ sessionId, oldSandboxId, newSandboxId, responseId, nextResponseId,
      responses: sessionResponseRows(sessionId), afterInputs,
      nativeBefore, nativeAfter: nativeRootRows(sessionId) }),
    contentType: 'application/json',
  });
});
