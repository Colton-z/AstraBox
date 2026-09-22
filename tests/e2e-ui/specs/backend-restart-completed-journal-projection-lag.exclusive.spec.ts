/** An existing completed journal repairs its stale snapshot after a real backend restart. */
import { execFileSync } from 'node:child_process';

import { expect, test } from '@playwright/test';

import { AstraApi, messageText, visibleMessages } from '../fixtures/astraApi';
import { framesForTurn, patchSnapshotDoc, sessionEvents, snapshotDoc } from '../fixtures/dbOracle';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

const sessions = trackSessions();
let sessionId = '';
let turnId = '';
const evidence: Record<string, unknown> = {};

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`expected an object, received ${JSON.stringify(value)}`);
  }
  return value as Record<string, unknown>;
}

function terminalEvents(): Record<string, unknown>[] {
  return sessionEvents(sessionId).filter((event) => event.turn_id === turnId
    && ['turn.completed', 'turn.failed', 'turn.recovered'].includes(String(event.event_type)));
}

function serverCommand(container: string, args: string[]): string {
  return execFileSync('docker', [...args, container], {
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
  await info.attach('completed-journal-projection-lag-scene', {
    body: JSON.stringify({ sessionId, turnId, ...evidence,
      snapshot: await observe(() => snapshotDoc(sessionId)),
      events: await observe(() => sessionEvents(sessionId)),
      frames: await observe(() => framesForTurn(turnId)),
      session: await observe(() => api.getSession(sessionId)),
      history: await observe(() => api.getMessages(sessionId, 100)),
    }),
    contentType: 'application/json',
  });
});

test('backend restart repairs a completed journal projection without a second terminal', async ({ page, request }) => {
  const api = new AstraApi(request);
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const agent = await api.defaultAgent();
  sessionId = (await api.startConversation(agent.agent_id)).session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);
  await openSessionView(page, sessionId);
  const prompt = `In one short sentence, explain why a bookmark helps a reader. Do not use tools. Reference: ${Date.now()}.`;
  await sendPrompt(page, sessionId, prompt);
  const baseline = await api.waitForSession(sessionId, (row) => row.state === 'READY'
    && !row.current_turn_id && row.last_turn_status === 'COMPLETED' && Boolean(row.last_turn_id));
  expect(baseline.last_error || null).toBeNull();
  turnId = String(baseline.last_turn_id);
  const history = await api.getMessages(sessionId, 100);
  const messages = visibleMessages(history);
  expect(messages.filter((row) => row.role === 'user').map(messageText)).toEqual([prompt]);
  const replies = messages.filter((row) => row.role === 'assistant' && row.turn_id === turnId);
  expect(replies).toHaveLength(1);
  const reply = messageText(replies[0]);
  expect(reply.trim()).not.toBe('');
  expect(replies[0].blocks?.filter((block) => block.type === 'tool_use') || []).toEqual([]);
  const replyBubble = page.locator(`[data-message-id="${replies[0].message_id}"]`)
    .getByTestId('assistant-message');
  await expect(replyBubble).toHaveCount(1);
  await expect(replyBubble).toContainText(reply);
  await expect(page.getByTestId('assistant-message')).toHaveCount(1);

  const originalSnapshot = object(snapshotDoc(sessionId));
  const originals = terminalEvents();
  expect(originals).toHaveLength(1);
  expect(originals[0]).toMatchObject({ event_type: 'turn.completed', turn_id: turnId });
  const completedSeq = Number(originals[0].event_seq);
  expect(completedSeq).toBeGreaterThan(0);
  expect(Number(originalSnapshot.conversation_event_seq_applied)).toBeGreaterThanOrEqual(completedSeq);
  expect(originalSnapshot).toMatchObject({ conversation_state: 'IDLE', last_turn_status: 'COMPLETED' });
  const terminal = object(originalSnapshot.last_turn_terminal_frame);
  expect(terminal).toMatchObject({ turn_id: turnId, type: 'finish', finish_reason: 'stop' });
  evidence.baseline = { originalSnapshot, originals, history };

  // No server writer may repair the fault before the actual restart boundary.
  try {
    serverCommand(server, ['stop', '--time', '10']);
    expect(serverCommand(server, ['inspect', '--format', '{{.State.Running}}'])).toBe('false');
    patchSnapshotDoc(sessionId, {
      conversation_state: 'PROCESSING', current_turn_id: turnId,
      current_turn_worker_command_id: null,
      last_turn_id: null, last_turn_status: null, last_turn_error: null,
      last_turn_command_id: null, last_turn_failure_phase: null,
      last_turn_terminal_frame: null, last_turn_terminal_reason: null,
      turn_recovery_phase: null, active_interaction_id: null,
      delivery_state: 'RECEIVED',
      worker_heartbeat_at: '2024-01-01T00:00:00+00:00',
      updated_at: '2024-01-01T00:00:00+00:00',
      conversation_event_seq_applied: completedSeq - 1,
    });
    const fault = object(snapshotDoc(sessionId));
    evidence.fault = fault;
    expect(fault).toMatchObject({ conversation_state: 'PROCESSING', current_turn_id: turnId,
      conversation_event_seq_applied: completedSeq - 1 });
    expect(fault.last_turn_terminal_frame).toBeUndefined();
    expect(terminalEvents(), 'fault injection must retain the actual completion').toEqual(originals);
    expect(framesForTurn(turnId).some((frame) => Number(frame.event_seq) === Number(terminal.frame_seq)
      && object(frame.payload).type === 'finish')).toBe(true);
  } finally {
    // Restore only the shared service, including on failed injection; keep the test's data.
    serverCommand(server, ['start']);
  }

  await expect.poll(async () => {
    try { await api.getSession(sessionId); return true; }
    catch (error) { evidence.lastReadinessError = String(error); return false; }
  }, { timeout: 60_000, message: 'the restarted backend must serve the original Session' }).toBe(true);
  await openSessionView(page, sessionId);
  await expect.poll(() => {
    const snapshot = object(snapshotDoc(sessionId));
    return { state: snapshot.conversation_state, turn: snapshot.last_turn_id,
      status: snapshot.last_turn_status, watermark: snapshot.conversation_event_seq_applied };
  }, { timeout: 30_000, message: 'replay the original completion into the lagging snapshot' })
    .toEqual({ state: 'IDLE', turn: turnId, status: 'COMPLETED', watermark: completedSeq });
  const repaired = object(snapshotDoc(sessionId));
  expect(repaired.last_turn_error || null).toBeNull();
  expect(repaired.turn_recovery_phase || null).toBeNull();
  expect(repaired.current_turn_remote_anchor || null).toBeNull();
  expect(repaired.current_turn_id || null).toBeNull();
  expect(repaired.last_turn_terminal_frame).toEqual(terminal);
  expect(terminalEvents(), 'repair must not append a new completed, failed or recovered event').toEqual(originals);
  const detail = await api.getSession(sessionId);
  expect(detail).toMatchObject({ state: 'READY', last_turn_id: turnId, last_turn_status: 'COMPLETED' });
  expect(detail.last_error || null).toBeNull();
  expect(detail.pending_interaction || null).toBeNull();
  const cold = visibleMessages(await api.getMessages(sessionId, 100));
  expect(cold.filter((row) => row.role === 'user').map(messageText)).toEqual([prompt]);
  expect(cold.filter((row) => row.role === 'assistant').map((row) => ({ turn: row.turn_id, text: messageText(row) })))
    .toEqual([{ turn: turnId, text: reply }]);
  await expect(replyBubble).toHaveCount(1);
  await expect(replyBubble).toContainText(reply);
  await expect(page.getByTestId('assistant-message')).toHaveCount(1);
  await expect(page.getByTestId('pending-interaction-panel')).toHaveCount(0);
  await expect(page.getByTestId('run-view').locator('header').getByTestId('status-pill')).toHaveText(/Ready|就绪/);
  test.info().annotations.push({ type: 'journal-projection-repair', description: JSON.stringify({
    sessionId, turnId, completedSeq, terminal,
  }) });
});
