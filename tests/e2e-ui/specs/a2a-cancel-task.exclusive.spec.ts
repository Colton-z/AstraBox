/**
 * A2A CancelTask stops the work a task's message started, on the conversation's own records.
 *
 * Canceled while its command runs, the task's response ends `cancelled` and
 * the command never reaches its end. Canceled while the conversation is still
 * starting, its message is withdrawn before the runtime takes it: no response
 * answers it, the Agent's own transcript never holds it, and the conversation
 * page shows it as cancelled by its sender.
 */
import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { nativeTranscriptRows, sessionInputRows, sessionResponseRows } from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
import { openSessionView } from '../fixtures/sessionPage';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

type Part = { text?: string; data?: Record<string, unknown> };
type Task = { id: string; contextId: string; status: { state: string; message?: { parts: Part[] } };
  metadata: { session_id: string; input_id: string; response_id: string } };
type RpcError = { code: number; message: string; data?: unknown };

const sessions = trackSessions();
const agents: string[] = [];
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  for (const id of agents) await api.deleteAgent(id);
});

function budget() {
  const deadline = Date.now() + 155_000;
  return () => {
    const left = deadline - Date.now();
    expect(left, 'the cancellation journey shares the fixed business budget').toBeGreaterThan(0);
    return left;
  };
}

async function call<T>(request: APIRequestContext, agentId: string, method: string, params: object):
  Promise<{ result?: T; error?: RpcError }> {
  const response = await request.post(apiPath(`/agents/${agentId}/a2a`), {
    headers: { 'A2A-Version': '1.0' },
    data: { jsonrpc: '2.0', id: randomUUID(), method, params },
  });
  expect(response.ok(), await response.text()).toBe(true);
  return response.json();
}

async function rpc<T>(request: APIRequestContext, agentId: string, method: string, params: object): Promise<T> {
  const body = await call<T>(request, agentId, method, params);
  expect(body.error, JSON.stringify(body)).toBeUndefined();
  return body.result as T;
}

function send(text: string) {
  return { message: { messageId: randomUUID(), role: 'ROLE_USER', parts: [{ text }] },
    configuration: { historyLength: 0, returnImmediately: true } };
}

async function canceled(request: APIRequestContext, agentId: string, task: Task, remaining: () => number) {
  let latest = task;
  await expect.poll(async () => {
    latest = await rpc<Task>(request, agentId, 'GetTask', { id: task.id });
    return latest.status.state;
  }, { message: 'the canceled task ends canceled', timeout: Math.min(60_000, remaining()), intervals: [500] })
    .toBe('TASK_STATE_CANCELED');
  return latest;
}

test('a task canceled while its command runs ends with its response, and the command never finishes', async ({ request }) => {
  const remaining = budget();
  const api = new AstraApi(request);
  const marker = `CANCEL_RUN_${randomUUID().replaceAll('-', '')}`;
  const agentId = (await api.createColdTestAgent(`__e2e_${marker}`)).agent_id;
  agents.push(agentId);
  const prompt = [
    'Use Bash exactly once to execute this command synchronously; do not run it in the background:',
    `printf '%s' '${marker}' > '${marker}.started'; until test -f '${marker}.release'; do sleep 0.2; done; printf '%s' '${marker}' > '${marker}.txt'`,
    `Then reply with exactly ${marker}. All files must be in your workspace.`,
    'Use Bash timeout 120000 and run_in_background false. The client will provide the release file; do not create it yourself.',
  ].join('\n');
  // SendMessage answers with a task or a message; this server always answers with the task.
  const task = (await rpc<{ task: Task }>(request, agentId, 'SendMessage', send(prompt))).task;
  const sessionId = task.metadata.session_id;
  sessions.push(sessionId);
  await expect.poll(async () => {
    try { return await api.downloadFileText(sessionId, `/workspace/${marker}.started`, Math.min(10_000, remaining())); }
    catch (error) {
      if (/files\/download -> (404|409)/.test(String(error))) return '';
      throw error;
    }
  }, { message: 'the command runs', timeout: Math.min(120_000, remaining()), intervals: [500] }).toBe(marker);

  const cancel = await call<Task>(request, agentId, 'CancelTask', { id: task.id });
  expect(cancel.error, JSON.stringify(cancel)).toBeUndefined();
  const ended = await canceled(request, agentId, task, remaining);
  const responseId = ended.metadata.response_id;
  expect(responseId, 'the canceled task names the response it started').not.toBe('');
  expect(sessionResponseRows(sessionId).filter((row) => row.response_id === responseId))
    .toEqual([expect.objectContaining({ status: 'ended', outcome: 'cancelled', input_ids: [ended.metadata.input_id] })]);

  // Released now, a command still running would write its result file.
  await api.uploadFileText(sessionId, '/workspace', `${marker}.release`, 'release', Math.min(10_000, remaining()));
  await new Promise((resolve) => setTimeout(resolve, 5_000));
  await expect(api.downloadFileText(sessionId, `/workspace/${marker}.txt`, Math.min(10_000, remaining())),
    'the canceled command never reached its end').rejects.toThrow(/files\/download -> 404/);
  const again = await call<Task>(request, agentId, 'CancelTask', { id: task.id });
  expect(again.error?.message ?? '', 'an ended task is not canceled again').toMatch(/already ended/);
});

test('a task canceled before its conversation\'s runtime takes it is withdrawn, never reaching the Agent', async ({ request, page }) => {
  const remaining = budget();
  const api = new AstraApi(request);
  const marker = `CANCEL_HELD_${randomUUID().replaceAll('-', '')}`;
  const agentId = (await api.createColdTestAgent(`__e2e_${marker}`)).agent_id;
  agents.push(agentId);
  const task = (await rpc<{ task: Task }>(request, agentId, 'SendMessage', send(`Reply with exactly ${marker}.`))).task;
  const sessionId = task.metadata.session_id;
  sessions.push(sessionId);
  // A conversation of a cold Agent is still creating its box: nothing has the message yet.
  expect(sessionInputRows(sessionId)).toEqual([expect.objectContaining({ input_id: task.metadata.input_id, state: 'pending' })]);

  // No reader holds the conversation yet: the one that takes it applies the
  // cancellation before it delivers anything, so the call answers at once.
  const cancel = await call<Task>(request, agentId, 'CancelTask', { id: task.id });
  expect(cancel.error, JSON.stringify(cancel)).toBeUndefined();
  const ended = await canceled(request, agentId, task, remaining);
  expect(ended.status.message?.parts.map((part) => part.text).join(''), 'the task says who withdrew it')
    .toContain('Cancelled by its sender');

  const input = sessionInputRows(sessionId).find((row) => row.input_id === task.metadata.input_id);
  expect(input, 'the withdrawn message stays in the conversation').toMatchObject({
    state: 'cancelled', response_id: null, failure: expect.objectContaining({ code: 'INPUT_CANCELLED' }) });
  expect(sessionResponseRows(sessionId), 'nothing answered the withdrawn message').toEqual([]);
  expect(nativeTranscriptRows(sessionId).some((row) => row.entry_json.includes(marker)),
    'the Agent never received the withdrawn message').toBe(false);

  await openSessionView(page, sessionId);
  await expect(page.getByTestId('user-message-cancelled')).toBeVisible({ timeout: Math.min(30_000, remaining()) });
  await expect(page.getByText(`Reply with exactly ${marker}.`)).toBeVisible();
});
