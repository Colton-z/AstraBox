/**
 * User history flushed before the SDK Result must survive loss of the sandbox
 * that was still running the tool. This pins the eager SessionStore boundary,
 * then reloads the cold page and rebuilds on a fresh box.
 */
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import {
  documentsByField,
  framesForTurn,
  lapseSessionSandboxLease,
  sessionDoc,
  sessionEvents,
  snapshotDoc,
  waitForTurnSnapshot,
  waitForTurnTerminalProof,
} from '../fixtures/dbOracle';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import { browserStreamFrames, nativeRootEntries, object } from '../fixtures/nativeMcpServer';
import {
  killSandbox,
  requireSandboxHandle,
  sandboxExec,
  sandboxRunning,
  waitForSandboxStopped,
} from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { mirrorSseBodies } from '../fixtures/sseBodies';

const TURN_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_REBORROW_TURN_TIMEOUT_MS', 240_000);
const KILL_CONVERGE_MS = parseTimeoutEnv('ASTRABOX_E2E_KILL_CONVERGE_MS', 30_000);

interface TranscriptEntryDoc extends Record<string, unknown> {
  entry_json?: string;
  uuid?: string;
}

let agentId = '';
const sessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

function userEntriesWithMarker(sessionId: string, marker: string): TranscriptEntryDoc[] {
  return documentsByField('transcript_entries', '$.platform_session_id', sessionId)
    .map((row) => row as TranscriptEntryDoc)
    .filter((row) => {
      if (typeof row.entry_json !== 'string') return false;
      try {
        const entry = JSON.parse(row.entry_json) as Record<string, unknown>;
        return String(entry.type || '') === 'user' && JSON.stringify(entry).includes(marker);
      } catch {
        return false;
      }
    });
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
  expect(oldSandboxId, 'the active turn must have a sandbox to lose').not.toEqual('');
  const workspace = String(ready.terminal_cwd || '').trim();
  expect(workspace, 'a READY session must publish its workspace').toMatch(/^\/.+/);
  // The fixture and the agent share this box's workspace, not their private /tmp.
  const startedPath = `${workspace}/astrabox-pre-result-${runId}.started`;
  const holdPath = `${workspace}/astrabox-pre-result-${runId}.hold`;
  const oldSandbox = await requireSandboxHandle(api, oldSandboxId);
  sandboxExec(oldSandbox, `touch ${holdPath}`);
  expect(sandboxExec(oldSandbox, `test -f ${holdPath} && echo yes`).trim()).toBe('yes');

  // Only the old box receives permission to block. Replaying the unanswered
  // input on a volume-free replacement must not recreate that permission.
  const prompt = [
    userMarker,
    'Use the Bash tool exactly once to run this command verbatim:',
    '```bash',
    `if [ -f ${holdPath} ]; then touch ${startedPath}; while [ -f ${holdPath} ]; do sleep 0.2; done; fi; echo released`,
    '```',
    'Do not use another tool. Wait for Bash before replying.',
  ].join('\n');
  const streaming = api.streamPrompt(sessionId, prompt, undefined, TURN_BUDGET_MS)
    .catch(() => '');

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
  const originalTurnId = String(snapshotDoc(sessionId)?.current_turn_id || '').trim();
  expect(originalTurnId, 'the blocked tool must belong to an active platform turn').not.toBe('');

  killSandbox(oldSandbox);
  await waitForSandboxStopped(oldSandbox, KILL_CONVERGE_MS);
  await streaming;
  await api.waitForSession(
    sessionId,
    (session) => session.state === 'READY' && !String(session.current_turn_id || '').trim(),
    TURN_BUDGET_MS,
  );
  await waitForTurnTerminalProof(sessionId, originalTurnId, 'FAILED');

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
  expect(result.errorText, 'the next turn must rebuild without an error frame').toBeNull();
  await expect.poll(async () => {
    const history = await api.getMessages(sessionId, 100);
    return history.messages.filter(
      (message) => message.role === 'assistant' && messageText(message).includes(replyMarker),
    ).length;
  }, {
    timeout: TURN_BUDGET_MS,
    message: 'the rebuilt turn must persist one exact reply',
  }).toBe(1);
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
  expect(JSON.parse(retainedOriginal[0].entry_json!)).toEqual(JSON.parse(originalUserEntry.entry_json!));
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
  const inputs = () => sessionEvents(sessionId).filter((row) => row.event_type === 'command.accepted'
    && object(row.payload).command_type === 'StartTurn');
  const nativeUsers = () => nativeRootEntries(sessionId).filter((row) => row.entry.type === 'user'
    && JSON.stringify(row.entry).includes(historyMarker));

  await mirrorSseBodies(page);
  await openSessionView(page, sessionId);
  const firstResponse = await sendPrompt(page, sessionId, prompt);
  const firstReceipt = object(object(await firstResponse.json()).data);
  const pending = await api.waitForPendingInteraction(sessionId, TURN_BUDGET_MS);
  expect(pending.presentation).toBe('tool_approval');
  expect(pending.tool_name).toBe('Write');
  const turnId = String(pending.turn_id || '').trim();
  const interactionId = String(pending.interaction_id || '').trim();
  const toolId = String(pending.tool_call_id || '').trim();
  for (const id of [turnId, interactionId, toolId]) expect(id).not.toBe('');
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
  const paused = await waitForTurnSnapshot(sessionId, turnId);
  expect(paused.conversation_state).toBe('WAITING_FOR_INTERACTION');
  expect(paused.active_interaction_id).toBe(interactionId);
  const interactionRows = () => documentsByField('interaction_snapshots', '$.session_id', sessionId)
    .filter((row) => row.interaction_id === interactionId);
  expect(interactionRows()).toHaveLength(1);
  expect(interactionRows()[0]).toMatchObject({ engine_kind: 'claude_code', interaction_state: 'OPEN', active: true });
  await expect.poll(() => nativeUsers().length, {
    message: 'the real pending Write input must reach database custody before any SDK Result',
  }).toBe(1);
  const nativeToolCalls = () => nativeRootEntries(sessionId).flatMap((row) => {
    if (row.entry.type !== 'assistant') return [];
    const content = object(row.entry.message).content;
    return Array.isArray(content) ? content.map(object).filter((block) => block.type === 'tool_use'
      && block.id === toolId && block.name === 'Write') : [];
  });
  await expect.poll(() => nativeToolCalls().length).toBe(1);
  expect(String(object(nativeToolCalls()[0].input).file_path)).toBe(filePath);
  const nativeBefore = nativeRootEntries(sessionId);
  const originalInputs = inputs();
  expect(originalInputs).toHaveLength(1);
  expect(originalInputs[0]).toMatchObject({ turn_id: turnId, causation_id: firstReceipt.command_id });
  expect(object(originalInputs[0].payload).content).toBe(prompt);
  const nativeTerminals = (id: string) => sessionEvents(sessionId).filter((row) => row.turn_id === id
    && row.event_type === 'engine.diagnostic' && object(row.payload).event_type === 'engine.terminal');
  expect(nativeTerminals(turnId)).toEqual([]);
  expect(framesForTurn(turnId).filter((row) => object(row.payload).type === 'data-result')).toEqual([]);

  // With every browser observer gone, neither a detail GET nor a new send can
  // be mistaken for autonomous convergence of the dead, pending turn.
  await page.goto('about:blank');
  expect(sessionDoc(sessionId)?.sandbox_id).toBe(oldSandboxId);
  killSandbox(oldSandbox);
  await waitForSandboxStopped(oldSandbox, KILL_CONVERGE_MS);
  expect(sandboxRunning(oldSandbox)).toBe(false);
  const expiredAt = new Date(Date.now() - 60 * 60 * 1_000).toISOString();
  const lapse = lapseSessionSandboxLease(sessionId, oldSandboxId, expiredAt);
  await test.info().attach('pending-write-pre-result-fault', {
    body: JSON.stringify({ sessionId, oldSandboxId, turnId, interactionId, toolId, pending,
      firstReceipt, originalInputs, nativeBefore, expiredAt, lapse }),
    contentType: 'application/json',
  });
  if (lapse.length) expect(lapse).toEqual([{ session_id: sessionId, sandbox_id: oldSandboxId, expires_at: expiredAt }]);
  await expect.poll(() => {
    const session = sessionDoc(sessionId);
    const snapshot = snapshotDoc(sessionId);
    return { state: session?.state, sandbox: session?.sandbox_id || null,
      turn: session?.current_turn_id || null, unavailable: session?.runtime_unavailable,
      pending: Boolean(session?.pending_interaction), activeInteraction: snapshot?.active_interaction_id || null,
      originalInteractionActive: interactionRows()[0]?.active };
  }, { timeout: KILL_CONVERGE_MS, intervals: [500, 1_000],
    message: 'background lifecycle convergence must unbind dead compute and invalidate the old pending interaction without user action' })
    .toEqual({ state: 'READY', sandbox: null, turn: null, unavailable: true,
      pending: false, activeInteraction: null, originalInteractionActive: false });
  await waitForTurnTerminalProof(sessionId, turnId, 'FAILED', KILL_CONVERGE_MS);
  const convergenceEvents = () => sessionEvents(sessionId).filter((row) => {
    if (row.event_type !== 'session.lifecycle_reconciled') return false;
    const payload = object(row.payload);
    const reason = String(payload.reason || '');
    return payload.state === 'READY' && payload.runtime_unavailable === true
      && (reason === 'sandbox_terminating_notice' || reason.startsWith('dead_binding_reconcile:')
        || reason.startsWith('sandbox_callback_') || reason === 'engine control attach:SANDBOX_GONE');
  });
  await expect.poll(() => convergenceEvents().length).toBe(1);
  const convergence = convergenceEvents()[0];
  expect(inputs()).toEqual(originalInputs);
  expect(nativeRootEntries(sessionId).slice(0, nativeBefore.length)).toEqual(nativeBefore);
  await test.info().attach('pending-write-background-convergence', {
    body: JSON.stringify({ sessionId, oldSandboxId, convergence, interaction: interactionRows()[0] }),
    contentType: 'application/json',
  });

  await openSessionView(page, sessionId);
  await expect(page.getByTestId('pending-interaction-panel')).toHaveCount(0);
  await expect(firstUser).toHaveCount(1);
  await expect(firstUser).toHaveText(renderedFirstUser, { useInnerText: true });
  const coldHistory = await api.getMessages(sessionId, 100);
  expect(coldHistory.has_more).toBe(false);
  expect(coldHistory.messages.filter((message) => message.role === 'user').map(messageText)).toEqual([prompt]);

  const nextPrompt = `Do not use tools. Reply with exactly ${replyMarker}.`;
  const nextResponse = await sendPrompt(page, sessionId, nextPrompt);
  const nextReceipt = object(object(await nextResponse.json()).data);
  await expect.poll(() => inputs().filter((row) => row.causation_id === nextReceipt.command_id).length).toBe(1);
  const nextCommand = inputs().find((row) => row.causation_id === nextReceipt.command_id)!;
  const nextTurnId = String(nextCommand.turn_id || '').trim();
  expect(nextTurnId).not.toBe('');
  expect(nextTurnId).not.toBe(turnId);
  const terminal = await waitForTurnTerminalProof(sessionId, nextTurnId, 'COMPLETED', TURN_BUDGET_MS);
  // The same diagnostic query must positively identify the real completed SDK
  // turn; pre-fault absence alone cannot validate a terminal-evidence oracle.
  await expect.poll(() => nativeTerminals(nextTurnId).length).toBe(1);
  expect(nativeTerminals(nextTurnId)[0].causation_id).toBe(nextReceipt.command_id);
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
  expect(rebuilt.last_turn_id).toBe(nextTurnId);
  expect(rebuilt.last_turn_status).toBe('COMPLETED');
  const nextFrames = async () => {
    const frames = await browserStreamFrames(page);
    const start = frames.findIndex((frame) => frame.type === 'data-input-consumed'
      && object(frame.data).inputId === nextReceipt.input_id);
    return start < 0 ? [] : frames.slice(start);
  };
  await expect.poll(async () => (await nextFrames()).filter((frame) => frame.type === 'data-result').length).toBe(1);
  const frames = await nextFrames();
  expect(frames.filter((frame) => frame.type === 'error' || frame.type === 'unparsed')).toEqual([]);
  expect(frames.filter((frame) => frame.type === 'data-input-consumed')).toHaveLength(1);
  expect(frames.filter((frame) => frame.type === 'text-delta').map((frame) => String(frame.delta || '')).join(''))
    .toContain(replyMarker);
  expect(framesForTurn(nextTurnId).filter((row) => object(row.payload).type === 'error')).toEqual([]);
  const afterInputs = inputs();
  expect(afterInputs).toHaveLength(2);
  expect(afterInputs[0]).toEqual(originalInputs[0]);
  expect(object(afterInputs[1].payload).content).toBe(nextPrompt);
  // Retain the complete ordered native prefix, including metadata without a
  // UUID. Additional supplier-owned resume records are not platform inputs.
  expect(nativeRootEntries(sessionId).slice(0, nativeBefore.length)).toEqual(nativeBefore);
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
    body: JSON.stringify({ sessionId, oldSandboxId, newSandboxId, turnId, nextTurnId,
      convergence, terminal: terminal.last_turn_terminal_frame, afterInputs,
      nativeBefore, nativeAfter: nativeRootEntries(sessionId) }),
    contentType: 'application/json',
  });
});
