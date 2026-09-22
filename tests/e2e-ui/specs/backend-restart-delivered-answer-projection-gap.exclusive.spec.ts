/** A delivered approval survives missing local answer and terminal projections across restart. */
import { execFileSync } from 'node:child_process';

import { expect, test } from '@playwright/test';

import { AstraApi, messageText, visibleMessages } from '../fixtures/astraApi';
import {
  deleteSessionEvents, documentsByField, framesForTurn,
  replaceDocs, sessionEvents, snapshotDoc,
} from '../fixtures/dbOracle';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

const sessions = trackSessions();
let sessionId = '';
let turnId = '';
let interactionId = '';
const evidence: Record<string, unknown> = {};

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`expected object, received ${JSON.stringify(value)}`);
  }
  return value as Record<string, unknown>;
}

function interaction(): Record<string, unknown> {
  const rows = documentsByField('interaction_snapshots', '$.session_id', sessionId)
    .filter((row) => row.interaction_id === interactionId);
  expect(rows).toHaveLength(1);
  return rows[0];
}

function nativeRows(): Array<{ seq: unknown; subpath: unknown; entry_json: unknown }> {
  return documentsByField('transcript_entries', '$.platform_session_id', sessionId)
    .map((row) => ({ seq: row.seq, subpath: row.subpath ?? null, entry_json: row.entry_json }));
}

function nativeRootBlocks(): Record<string, unknown>[] {
  return nativeRows().filter((row) => row.subpath === null).flatMap((row) => {
    const entry = object(JSON.parse(String(row.entry_json)));
    if (entry.type !== 'assistant' && entry.type !== 'user') return [];
    const content = object(entry.message).content;
    return Array.isArray(content) ? content.map(object) : [];
  });
}

function nativeWrites(): Record<string, unknown>[] {
  return nativeRootBlocks().filter((block) => block.type === 'tool_use' && block.name === 'Write');
}

function serverCommand(server: string, args: string[]): string {
  return execFileSync('docker', [...args, server], {
    encoding: 'utf8', timeout: 30_000, stdio: ['ignore', 'pipe', 'pipe'],
  }).trim();
}

async function observe(read: () => unknown | Promise<unknown>): Promise<unknown> {
  try { return await read(); }
  catch (error) { return { unavailable: String(error) }; }
}

test.afterEach(async ({ request }, info) => {
  if (!['failed', 'timedOut', 'interrupted'].includes(String(info.status))) return;
  const api = new AstraApi(request);
  await info.attach('delivered-answer-projection-gap-scene', {
    body: JSON.stringify({ sessionId, turnId, interactionId, ...evidence,
      session: await observe(() => api.getSession(sessionId)),
      history: await observe(() => api.getMessages(sessionId, 100)),
      snapshot: await observe(() => snapshotDoc(sessionId)),
      interaction: await observe(interaction),
      events: await observe(() => sessionEvents(sessionId)),
      frames: await observe(() => framesForTurn(turnId)),
      native: await observe(nativeRows),
    }), contentType: 'application/json',
  });
});

test('backend restart recovers a delivered answer without asking or executing its tool again', async ({ page, request }) => {
  const api = new AstraApi(request);
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const agent = await api.defaultAgent();
  sessionId = (await api.startConversation(agent.agent_id)).session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);
  await api.setPermissionMode(sessionId, 'default');
  await openSessionView(page, sessionId);
  const runId = Date.now();
  const file = `e2e-delivered-answer-${runId}.txt`;
  const content = `delivered approval ${runId}`;
  const marker = `ANSWER_COMMITTED_${runId}`;
  const prompt = `Call Write exactly once to create relative file ${file} with exactly this content: ${content}\n`
    + `Wait for its approval and tool result, then reply with ${marker}. Do not use any other tool.`;
  await sendPrompt(page, sessionId, prompt);
  const pending = await api.waitForPendingInteraction(sessionId, 60_000);
  expect(pending.tool_name).toBe('Write');
  turnId = String(pending.turn_id || '');
  interactionId = pending.interaction_id;
  const toolId = String(pending.tool_call_id || '');
  expect(turnId).not.toBe('');
  expect(toolId).not.toBe('');
  const openInteraction = interaction();
  expect(openInteraction).toMatchObject({ interaction_state: 'OPEN', active: true, turn_id: turnId });
  const openSnapshot = object(snapshotDoc(sessionId));
  const pendingFrames = framesForTurn(turnId).filter((frame) => {
    const payload = object(frame.payload);
    return payload.type === 'data-interaction' && object(payload.data).interaction_id === interactionId;
  });
  expect(pendingFrames).toHaveLength(1);
  const cutoff = Number(pendingFrames[0].event_seq);
  expect(Number.isSafeInteger(cutoff)).toBe(true);

  const response = page.waitForResponse((row) => row.request().method() === 'POST'
    && row.url().includes(`/sessions/${sessionId}/interaction-respond`));
  await page.getByTestId('pending-interaction-panel').getByRole('button', {
    name: /Allow and continue|Apply suggestion and continue|允许并继续|应用建议并继续/,
  }).last().click();
  const answered = await response;
  expect(answered.status()).toBe(200);
  const envelope = object(await answered.json());
  expect(object(envelope.data ?? envelope)).toMatchObject({ interaction_id: interactionId, answered: true });
  const settled = await api.waitForSession(sessionId, (row) => row.state === 'READY'
    && !row.current_turn_id && row.last_turn_id === turnId && row.last_turn_status === 'COMPLETED');
  expect(settled.last_error || null).toBeNull();
  expect(interaction()).toMatchObject({ interaction_state: 'ANSWERED', active: false });
  expect(await api.downloadFileText(sessionId, file)).toBe(content);
  const baselineHistory = visibleMessages(await api.getMessages(sessionId, 100));
  const baselineReplies = baselineHistory.filter((row) => row.role === 'assistant' && row.turn_id === turnId);
  expect(baselineReplies).toHaveLength(1);
  const baselineText = (baselineReplies[0].blocks || []).filter((block) => block.type === 'text')
    .map((block) => String(block.text)).join('');
  expect(baselineText).toContain(marker);
  await expect.poll(nativeWrites, { timeout: 15_000 }).toEqual([
    expect.objectContaining({ id: toolId, name: 'Write' }),
  ]);
  await expect.poll(() => {
    const blocks = nativeRootBlocks();
    return {
      results: blocks.filter((block) => block.type === 'tool_result' && block.tool_use_id === toolId
        && block.is_error !== true).length,
      answers: blocks.filter((block) => block.type === 'text' && String(block.text).includes(marker)).length,
    };
  }, { timeout: 15_000, message: 'the original tool result and final answer must reach native database custody' })
    .toEqual({ results: 1, answers: 1 });
  const baselineNative = nativeRows();
  expect(baselineNative.length).toBeGreaterThan(0);
  const initialEvents = sessionEvents(sessionId).filter((row) => row.turn_id === turnId);
  const commands = initialEvents.filter((row) => row.event_type === 'command.accepted'
    && object(row.payload).command_type === 'AnswerInteraction');
  expect(commands).toHaveLength(1);
  const commandId = String(commands[0].causation_id || '');
  expect(commandId).not.toBe('');
  expect(interaction().answer_command_id).toBe(commandId);
  const dispatches = initialEvents.filter((row) => row.event_type === 'dispatch.confirmed'
    && row.causation_id === commandId);
  expect(dispatches).toHaveLength(1);
  const answerEvents = initialEvents.filter((row) => row.event_type === 'interaction.answer_persisted'
    && row.causation_id === commandId);
  expect(answerEvents).toHaveLength(1);
  const terminals = initialEvents.filter((row) => ['turn.completed', 'turn.failed', 'turn.recovered']
    .includes(String(row.event_type)));
  expect(terminals.length).toBeGreaterThan(0);
  expect(terminals.every((row) => row.event_type === 'turn.completed')).toBe(true);
  const initialFrames = framesForTurn(turnId);
  const suffix = initialFrames.filter((row) => Number(row.event_seq) > cutoff);
  expect(suffix.some((row) => object(row.payload).type === 'finish')).toBe(true);
  const sequences = [...new Set([...answerEvents, ...terminals, ...suffix].map((row) => Number(row.event_seq)))];
  evidence.baseline = { openSnapshot, openInteraction, initialEvents, initialFrames, baselineHistory, baselineNative, cutoff, commandId };

  try {
    serverCommand(server, ['stop', '--time', '10']);
    expect(serverCommand(server, ['inspect', '--format', '{{.State.Running}}'])).toBe('false');
    evidence.removed = deleteSessionEvents(sessionId, sequences);
    expect(replaceDocs('interaction_snapshots', { '$.session_id': sessionId, '$.interaction_id': interactionId },
      openInteraction)).toHaveLength(1);
    const faultSnapshot = {
      ...object(snapshotDoc(sessionId)),
      conversation_state: 'IDLE', current_turn_id: null,
      current_turn_worker_command_id: null,
      current_turn_remote_anchor: openSnapshot.current_turn_remote_anchor ?? null,
      current_turn_engine_anchor: openSnapshot.current_turn_engine_anchor ?? null,
      last_turn_id: turnId, last_turn_status: 'FAILED', last_turn_error: 'stale processing detected',
      last_turn_failure_phase: 'post_dispatch', last_turn_command_id: commandId,
      last_turn_terminal_frame: null, last_turn_terminal_reason: null,
      turn_recovery_phase: 'TRANSCRIPT_PENDING', active_interaction_id: null,
      delivery_state: 'RECEIVED', conversation_event_seq_applied: Number(dispatches[0].event_seq),
      worker_heartbeat_at: '2024-01-01T00:00:00+00:00', updated_at: '2024-01-01T00:00:00+00:00',
    };
    expect(replaceDocs('session_snapshots', { '$.session_id': sessionId }, faultSnapshot)).toHaveLength(1);
    const remaining = sessionEvents(sessionId);
    expect(remaining.filter((row) => sequences.includes(Number(row.event_seq)))).toEqual([]);
    expect(framesForTurn(turnId)).toEqual(initialFrames.filter((row) => Number(row.event_seq) <= cutoff));
    expect(remaining.filter((row) => row.event_type === 'dispatch.confirmed' && row.causation_id === commandId))
      .toEqual(dispatches);
    expect(interaction()).toEqual(openInteraction);
    expect(nativeRows(), 'the fault must not manufacture or erase the native result').toEqual(baselineNative);
    evidence.faultSnapshot = snapshotDoc(sessionId);
    expect(evidence.faultSnapshot).toEqual(faultSnapshot);
    expect(object(evidence.faultSnapshot)).toMatchObject({ current_turn_id: null,
      last_turn_status: 'FAILED', turn_recovery_phase: 'TRANSCRIPT_PENDING' });
  } finally {
    serverCommand(server, ['start']);
  }

  await expect.poll(async () => {
    try { await api.getSession(sessionId); return true; }
    catch (error) { evidence.lastReadinessError = String(error); return false; }
  }, { timeout: 60_000, message: 'the restarted service must serve the original Session' }).toBe(true);
  await openSessionView(page, sessionId);
  const recovered = await api.waitForSession(sessionId, (row) => row.state === 'READY'
    && !row.current_turn_id && row.last_turn_id === turnId && row.last_turn_status === 'COMPLETED');
  expect(recovered.last_error || null).toBeNull();
  expect(recovered.pending_interaction || null).toBeNull();
  const repaired = object(snapshotDoc(sessionId));
  expect(repaired.turn_recovery_phase || null).toBeNull();
  expect(repaired.current_turn_remote_anchor || null).toBeNull();
  expect(repaired.current_turn_engine_anchor || null).toBeNull();
  expect(repaired.last_turn_error || null).toBeNull();
  expect(object(repaired.last_turn_terminal_frame)).toMatchObject({ turn_id: turnId, type: 'finish', finish_reason: 'stop' });
  expect(interaction()).toMatchObject({ interaction_state: 'ANSWERED', active: false, answer_command_id: commandId });
  const after = sessionEvents(sessionId).filter((row) => row.turn_id === turnId);
  expect(after.filter((row) => row.event_type === 'command.accepted' && row.causation_id === commandId)).toEqual(commands);
  expect(after.filter((row) => row.event_type === 'dispatch.confirmed' && row.causation_id === commandId)).toEqual(dispatches);
  expect(after.filter((row) => row.event_type === 'interaction.answer_persisted' && row.causation_id === commandId)).toHaveLength(1);
  expect(after.filter((row) => row.event_type === 'turn.recovered')).toHaveLength(1);
  expect(after.filter((row) => row.event_type === 'turn.failed')).toEqual([]);
  const history = visibleMessages(await api.getMessages(sessionId, 100));
  expect(history.filter((row) => row.role === 'user').map(messageText)).toEqual([prompt]);
  const replies = history.filter((row) => row.role === 'assistant');
  expect(replies).toHaveLength(1);
  expect(replies[0].turn_id).toBe(turnId);
  expect((replies[0].blocks || []).filter((block) => block.type === 'text')
    .map((block) => String(block.text)).join('')).toBe(baselineText);
  const blocks = replies.flatMap((row) => row.blocks || []);
  expect(blocks.filter((block) => block.type === 'tool_use')).toEqual([expect.objectContaining({ id: toolId, name: 'Write' })]);
  expect(blocks.filter((block) => block.type === 'tool_result')).toEqual([expect.objectContaining({ tool_use_id: toolId, is_error: false })]);
  expect(await api.downloadFileText(sessionId, file)).toBe(content);
  const afterNative = nativeRows();
  for (const original of baselineNative) expect(afterNative).toContainEqual(original);
  expect(nativeWrites()).toEqual([expect.objectContaining({ id: toolId, name: 'Write' })]);
  expect(replies[0].message_id).toBe(baselineReplies[0].message_id);
  const recoveredReply = page.locator(`[data-message-id="${baselineReplies[0].message_id}"]`)
    .getByTestId('assistant-message');
  await expect(recoveredReply).toHaveCount(1);
  await expect(recoveredReply).toContainText(marker);
  await expect(page.getByTestId('assistant-message')).toHaveCount(1);
  await expect(page.getByTestId('pending-interaction-panel')).toHaveCount(0);
  await expect(page.getByTestId('run-view').locator('header').getByTestId('status-pill')).toHaveText(/Ready|就绪/);
  test.info().annotations.push({ type: 'delivered-answer-recovery', description: JSON.stringify({
    sessionId, turnId, interactionId, commandId, toolId, removedEventSeqs: sequences,
  }) });
});
