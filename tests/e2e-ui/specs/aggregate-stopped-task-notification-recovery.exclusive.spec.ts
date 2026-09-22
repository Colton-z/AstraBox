/** An aggregate vendor notification cannot corrupt a completed child's durable recovery. */
import { execFileSync } from 'node:child_process';

import { expect, test } from '@playwright/test';

import { AstraApi, messageText, visibleMessages } from '../fixtures/astraApi';
import { deleteSessionEvents, documentsByField, replaceDocs, sessionEvents } from '../fixtures/dbOracle';
import { absoluteBaseUrl } from '../fixtures/env';
import { restartServerContainer } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

const sessions = trackSessions();
let sessionId = '';
let agentId = '';
const evidence: Record<string, unknown> = {};

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`expected object, received ${JSON.stringify(value)}`);
  }
  return value as Record<string, unknown>;
}

function nativeRows(): Record<string, unknown>[] {
  return documentsByField('transcript_entries', '$.platform_session_id', sessionId);
}

function materializations(openedSeq: number): Record<string, unknown>[] {
  return sessionEvents(sessionId).filter((event) => event.event_type === 'turn.background_tasks_materialized'
    && Number(object(event.payload).source_opened_event_seq) === openedSeq);
}

async function observe(read: () => unknown | Promise<unknown>): Promise<unknown> {
  try { return await read(); }
  catch (error) { return { unavailable: String(error) }; }
}

test.afterEach(async ({ request }, info) => {
  if (!['failed', 'timedOut', 'interrupted'].includes(String(info.status))) return;
  const api = new AstraApi(request);
  await info.attach('aggregate-notification-recovery-scene', {
    body: JSON.stringify({ sessionId, agentId, ...evidence,
      session: await observe(() => api.getSession(sessionId)),
      children: await observe(() => api.listChildRuns(sessionId)),
      history: await observe(() => api.getMessages(sessionId, 100)),
      events: await observe(() => sessionEvents(sessionId)),
      native: await observe(nativeRows),
    }), contentType: 'application/json',
  });
});
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

test('aggregate stopped notification preserves a completed child through real recovery and cold history', async ({ page, request }) => {
  const api = new AstraApi(request);
  const server = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const agent = await api.createColdTestAgent(`aggregate-stopped-${Date.now()}`);
  agentId = agent.agent_id;
  sessionId = (await api.startConversation(agentId)).session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId);
  await api.setPermissionMode(sessionId, 'bypassPermissions');
  const runId = Date.now();
  const parentMarker = `AGGREGATE_PARENT_${runId}`;
  const childMarker = `AGGREGATE_CHILD_${runId}`;
  const prompt = [
    'Launch exactly one Agent with run_in_background=true and subagent_type=general-purpose.',
    `The child must call Bash once with: sleep 4 && printf '${childMarker}\\n'.`,
    `After Bash returns, the child must include ${childMarker} in its final answer.`,
    `Do not wait or use TaskOutput. After launch, reply with ${parentMarker}. Use no other tools.`,
  ].join('\n');
  const turn = await api.sendTurn(sessionId, prompt);
  expect(turn.errorText).toBeNull();
  await expect.poll(async () => {
    const [session, children] = await Promise.all([api.getSession(sessionId), api.listChildRuns(sessionId)]);
    return { state: session.state, background: Boolean(session.background_task_state),
      children: children.child_runs.map((child) => ({ status: child.engine_status, closed: child.closed })) };
  }, { timeout: 60_000, message: 'one real background child must finish before the notification fault' })
    .toEqual({ state: 'READY', background: false, children: [{ status: 'completed', closed: true }] });
  const child = (await api.listChildRuns(sessionId)).child_runs[0];
  const childId = child.child_run_id;
  const baselineChild = await api.getChildRunMessages(sessionId, childId);
  expect(baselineChild.messages.filter((row) => row.role === 'assistant')
    .flatMap((row) => row.content).filter((block) => block.type === 'text')
    .map((block) => String(block.text)).join('')).toContain(childMarker);
  const baselineHistory = visibleMessages(await api.getMessages(sessionId, 100));
  expect(baselineHistory.filter((row) => row.role === 'user').map(messageText)).toEqual([prompt]);
  const parentReplies = baselineHistory.filter((row) => row.role === 'assistant' && messageText(row).includes(parentMarker));
  expect(parentReplies.length).toBeGreaterThan(0);
  const baselineEvents = sessionEvents(sessionId);
  const opened = baselineEvents.filter((event) => event.event_type === 'turn.background_tasks_opened');
  expect(opened).toHaveLength(1);
  const openedSeq = Number(opened[0].event_seq);
  const originalMaterializations = materializations(openedSeq);
  expect(originalMaterializations).toHaveLength(1);
  expect(object(originalMaterializations[0].payload).source).toBe('engine_detached_child');
  const oldMaterializedSeq = Number(originalMaterializations[0].event_seq);
  const engineRefs = object(opened[0].payload).engine_refs;
  const controlRefs = Object.keys(object(object(opened[0].payload).control_to_engine_ref ?? {}));
  expect(engineRefs).toEqual([expect.any(String)]);
  const knownTaskIds = [...engineRefs as string[], ...controlRefs];

  // The browser's native XML parser reads the vendor record; no platform parser or fake success is invoked.
  const readNotifications = (rows: Record<string, unknown>[]) => page.evaluate(({ rows, ids }) => rows.flatMap((row) => {
    if (row.subpath !== null) return [];
    const entry = JSON.parse(String(row.entry_json));
    if (entry.type !== 'user' || entry.origin?.kind !== 'task-notification'
      || entry.message?.role !== 'user' || typeof entry.message?.content !== 'string') return [];
    const document = new DOMParser().parseFromString(entry.message.content, 'application/xml');
    if (document.querySelector('parsererror')) throw new Error('native task notification has malformed XML');
    const root = document.documentElement;
    const taskIds = Array.from(root.querySelectorAll('task-id')).map((node) => node.textContent?.trim() || '');
    const toolIds = Array.from(root.querySelectorAll('tool-use-id')).map((node) => node.textContent?.trim() || '');
    if (root.tagName !== 'task-notification' || taskIds.length !== 1 || !ids.includes(taskIds[0])) return [];
    if (toolIds.length !== 1 || !toolIds[0]) throw new Error('original notification must own one tool-use-id');
    const status = root.querySelector('status')?.textContent?.trim();
    if (!['completed', 'failed', 'killed'].includes(status || '')) throw new Error('original notification is not terminal');
    return [{ row, entry, taskId: taskIds[0], toolId: toolIds[0] }];
  }), { rows, ids: knownTaskIds });
  let baselineNative: Record<string, unknown>[] = [];
  let notifications: Awaited<ReturnType<typeof readNotifications>> = [];
  await expect.poll(async () => {
    baselineNative = nativeRows();
    notifications = await readNotifications(baselineNative);
    return notifications.length;
  }, { timeout: 15_000, message: 'the original singular notification must reach native database custody' }).toBe(1);
  expect(notifications).toHaveLength(1);
  const original = notifications[0];
  const secondaryTaskId = `aggregate-secondary-${runId}`;
  const changedContent = await page.evaluate(({ primary, secondary }) => {
    const document = new DOMParser().parseFromString('<task-notification/>', 'application/xml');
    for (const [tag, text] of [['task-id', primary], ['task-id', secondary], ['status', 'stopped'], ['summary', '2 tasks stopped']]) {
      const node = document.createElement(tag);
      node.textContent = text;
      document.documentElement.appendChild(node);
    }
    return new XMLSerializer().serializeToString(document.documentElement);
  }, { primary: original.taskId, secondary: secondaryTaskId });
  const changedRow = { ...original.row, entry_json: JSON.stringify({ ...original.entry,
    message: { ...original.entry.message, content: changedContent } }) };
  const rowId = String(original.row._id || '');
  expect(rowId).not.toBe('');
  evidence.baseline = { child, baselineChild, baselineHistory, opened, originalMaterializations,
    originalNotification: original, secondaryTaskId };
  try {
    execFileSync('docker', ['stop', '--time', '10', server], { timeout: 30_000, stdio: 'pipe' });
    expect(execFileSync('docker', ['inspect', '--format', '{{.State.Running}}', server], {
      timeout: 10_000, encoding: 'utf8', stdio: 'pipe',
    }).trim()).toBe('false');
    expect(replaceDocs('transcript_entries', { '$.platform_session_id': sessionId, '$._id': rowId }, changedRow)).toEqual([original.row]);
    expect(nativeRows()).toEqual(baselineNative.map((row) => row._id === rowId ? changedRow : row));
    evidence.removed = deleteSessionEvents(sessionId, [oldMaterializedSeq]);
    expect(materializations(openedSeq)).toEqual([]);
    expect(sessionEvents(sessionId)).toEqual(baselineEvents.filter((row) => Number(row.event_seq) !== oldMaterializedSeq));
    evidence.changedNotification = changedRow;
  } finally {
    await restartServerContainer(absoluteBaseUrl(), 60_000, server);
  }

  await expect.poll(() => materializations(openedSeq).map((row) => ({
    fresh: Number(row.event_seq) > oldMaterializedSeq, source: object(row.payload).source,
  })), { timeout: 45_000, message: 'real recovery must consume the altered native stream and re-materialize the same manifest' })
    .toEqual([{ fresh: true, source: 'engine_detached_child' }]);
  const recovered = await api.getSession(sessionId);
  expect(recovered.state).toBe('READY');
  expect(recovered.last_error || null).toBeNull();
  expect(recovered.background_task_state || null).toBeNull();
  expect((await api.listChildRuns(sessionId)).child_runs).toEqual([child]);
  expect(await api.getChildRunMessages(sessionId, childId)).toEqual(baselineChild);
  const afterHistory = visibleMessages(await api.getMessages(sessionId, 100));
  expect(afterHistory.map((row) => ({ id: row.message_id, role: row.role, turn: row.turn_id, text: messageText(row) })))
    .toEqual(baselineHistory.map((row) => ({ id: row.message_id, role: row.role, turn: row.turn_id, text: messageText(row) })));
  expect(afterHistory.filter((row) => row.role === 'user').map(messageText)).toEqual([prompt]);
  expect(nativeRows()).toEqual(baselineNative.map((row) => row._id === rowId ? changedRow : row));
  const afterEvents = sessionEvents(sessionId);
  expect(afterEvents.filter((row) => row.event_type === 'command.accepted'))
    .toEqual(baselineEvents.filter((row) => row.event_type === 'command.accepted'));
  expect(afterEvents.filter((row) => row.event_type === 'turn.completed'))
    .toEqual(baselineEvents.filter((row) => row.event_type === 'turn.completed'));
  await openSessionView(page, sessionId);
  await expect(page.getByTestId('user-message')).toHaveCount(1);
  await expect(page.getByTestId('user-message')).toContainText(prompt);
  await expect.poll(() => page.getByTestId('session-conversation').locator('[data-message-id]')
    .evaluateAll((elements) => elements.map((element) => element.getAttribute('data-message-id'))),
  { message: 'cold history preserves every original message exactly once and in order' })
    .toEqual(baselineHistory.map((row) => row.message_id));
  // A completion reply may quote the launch marker; identity, not shared wording, detects replay.
  for (const reply of parentReplies) {
    const bubble = page.locator(`[data-message-id="${reply.message_id}"]`).getByTestId('assistant-message');
    await expect(bubble).toHaveCount(1);
    await expect(bubble).toContainText(parentMarker);
  }
  await expect(page.getByTestId('user-message').filter({ hasText: '<task-notification>' })).toHaveCount(0);
  await page.getByRole('tab', { name: /^Agents/ }).click();
  const rows = page.getByTestId('subagent-agents-panel').getByTestId('subagent-agent-row');
  await expect(rows).toHaveCount(1);
  await expect(rows.getByText(/^completed$/)).toBeVisible();
  await rows.click();
  await expect(page.getByTestId('subagent-transcript-drawer')).toContainText(childMarker);
  test.info().annotations.push({ type: 'aggregate-notification-recovery', description: JSON.stringify({
    sessionId, childId, primaryTaskId: original.taskId, secondaryTaskId,
    openedSeq, oldMaterializedSeq, newMaterializedSeq: materializations(openedSeq)[0].event_seq,
  }) });
});
