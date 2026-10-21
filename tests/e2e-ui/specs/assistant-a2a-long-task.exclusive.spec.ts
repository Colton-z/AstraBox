/** Native process notifications deliver the result of one durable A2A task. */
import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi, messageText, messageToolCalls } from '../fixtures/astraApi';
import { sessionInputRows, sessionResponseRows } from '../fixtures/dbOracle';
import { apiPath, appPath } from '../fixtures/env';
import { agentTaskReferences, sessionRow } from '../fixtures/ingressRecords';
import { killSandbox, requireSandboxHandle } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

type Task = { id: string; contextId: string; status: { state: string };
  metadata: { session_id: string; input_id: string; response_id: string } };
const sessions = trackSessions();
let assistantId = '';
let agentId = '';
const evidence: Record<string, unknown> = {};
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  if (assistantId) await api.deleteAssistant(assistantId);
  if (agentId) await api.deleteAgent(agentId);
});
test.afterEach(async ({}, info) => {
  await info.attach('a2a-long-task-evidence', { body: JSON.stringify({ ...evidence, assistantId, agentId }),
    contentType: 'application/json' });
});

async function getTask(request: APIRequestContext, id: string): Promise<Task> {
  const response = await request.post(apiPath(`/agents/${agentId}/a2a`), {
    headers: { 'A2A-Version': '1.0' },
    data: { jsonrpc: '2.0', id: randomUUID(), method: 'GetTask', params: { id } },
  });
  expect(response.ok(), await response.text()).toBe(true);
  const body = await response.json();
  expect(body.error, JSON.stringify(body)).toBeUndefined();
  return body.result;
}

test('an Assistant receives one long A2A result after its watcher disconnects, and reports a failed task', async ({ page, request }) => {
  const deadline = Date.now() + 155_000;
  const remaining = () => Math.max(1, deadline - Date.now());
  const api = new AstraApi(request);
  const marker = randomUUID().replace(/-/g, '');
  const fast = `FAST_${marker}`;
  const ack = `STARTED_${marker}`;
  const filename = `a2a-${marker}.txt`;
  agentId = (await api.createColdTestAgent(`__e2e_a2a_long_${marker}`)).agent_id;
  const environment = process.env.ASTRABOX_E2E_ASSISTANT_ENVIRONMENT || await api.assistantEnvironmentName();
  assistantId = (await api.createAssistant({ display_name: `__e2e_a2a_source_${marker}`,
    runtime_profile_name: environment,
    model_config_override: { model_name: await api.assistantModelName(environment) },
  })).assistant_id;
  const sourceId = (await api.startAssistantConversation(assistantId)).session_id;
  sessions.push(sourceId);
  evidence.sourceId = sourceId;
  await api.waitForSessionReady(sourceId, 90_000);

  const started = Date.now();
  const initial = await api.sendTurn(sourceId, [
    'Load the built-in astrabox-agent-task skill with skill_view. Discover the configured AstraBox peer',
    `for Agent ${agentId}. Use its script for three tasks, each with a NEW context (omit --context-id):`,
    `1. message ID quick-${marker}: ask the target to reply exactly ${fast}.`,
    `2. message ID long-${marker}: ask the target to run exactly one FOREGROUND Bash call with timeout=60000`,
    'and run_in_background=false: sleep 30 seconds, then use Python secrets.token_hex(12) to generate a fresh value,',
    `append that value followed by a newline to ${filename}, and reply with the generated value.`,
    `3. message ID failure-${marker}: ask the target to run foreground Bash sleep 60 with timeout=90000`,
    'and run_in_background=false. The test will destroy this third task\'s compute.',
    'Submit each task ONCE, then start its native terminal(background=true, notify=true) watcher.',
    'For fault injection only, prefix the FIRST watcher of task2 with timeout 2s. This disconnects observation,',
    'not the task. On notification query that same task, then start an ordinary notified background watcher',
    'if still active. Never resubmit work to obtain a result. Do not write the target file yourself.',
    `After submitting all three and starting their watchers, reply ${ack} and END your response immediately.`,
    'On native notifications fetch results with get_agent_task. Report the quick result, the actual generated',
    'value exactly once, and FAILED with TASK_STATE_FAILED and its real failure reason for task3.',
    'An observer interruption is not a failed task. Do not ask for another user message or call a2a_call.',
  ].join('\n'), Math.min(remaining(), 90_000));
  expect(initial.errorText, JSON.stringify(initial)).toBeNull();
  expect(initial.text).toContain(ack);
  await expect.poll(() => agentTaskReferences(agentId).filter((row) => row.kind === 'task').length,
    { timeout: Math.min(remaining(), 15_000) }).toBe(3);
  const references = agentTaskReferences(agentId).filter((row) => row.kind === 'task');
  const ref = (kind: string) => references.find((row) => row.message_id === `${kind}-${marker}`)!;
  const quickRef = ref('quick');
  const longRef = ref('long');
  const failedRef = ref('failure');
  for (const row of [quickRef, longRef, failedRef]) {
    expect(row).toBeDefined();
    expect(row.session_id).toBeTruthy();
    sessions.push(String(row.session_id));
    expect(row.user_id).toBe(sessionRow(sourceId)?.user_id);
    expect(sessionRow(String(row.session_id))).toMatchObject({ user_id: row.user_id, agent_id: agentId, source_type: 'a2a' });
  }
  expect(new Set(references.map((row) => row.session_id)).size).toBe(3);
  const targetId = String(longRef.session_id);
  const failedTarget = String(failedRef.session_id);
  const longId = String(longRef.reference_id);
  const failedId = String(failedRef.reference_id);
  evidence.references = references;
  await expect.poll(async () => (await getTask(request, failedId)).status.state,
    { timeout: Math.min(remaining(), 30_000) }).toBe('TASK_STATE_WORKING');
  const failedSandbox = (await api.getSession(failedTarget)).sandbox_id;
  expect(failedSandbox).toBeTruthy();
  killSandbox(await requireSandboxHandle(api, String(failedSandbox)));
  await page.goto(appPath('/agents'));
  await expect.poll(async () => (await getTask(request, longId)).status.state,
    { timeout: Math.min(remaining(), 60_000), intervals: [1_000] }).toBe('TASK_STATE_COMPLETED');
  const value = (await api.downloadFileText(targetId, `/workspace/${filename}`, 30_000)).trim();
  expect(value, 'the target file supplies the expected result independently of source-side projections').toMatch(/^[a-f0-9]{24}$/);
  expect(Date.now() - started).toBeGreaterThan(30_000);
  await expect.poll(async () => (await getTask(request, failedId)).status.state,
    { timeout: Math.min(remaining(), 60_000), intervals: [1_000] }).toBe('TASK_STATE_FAILED');
  expect((await getTask(request, String(quickRef.reference_id))).status.state).toBe('TASK_STATE_COMPLETED');
  // Streaming text is visible before the native response ends. Require both.
  await expect.poll(async () => (await api.getMessages(sourceId)).messages.filter((row) => row.role === 'assistant'
    && row.metadata?.origin === 'engine' && row.metadata?.outcome === 'completed').map(messageText).join('\n'),
  { timeout: Math.min(remaining(), 60_000), intervals: [1_000] }).toContain(value);
  await expect.poll(async () => (await api.getMessages(sourceId)).messages.filter((row) => row.role === 'assistant'
    && row.metadata?.outcome === 'completed').map(messageText).join('\n'),
  { timeout: Math.min(remaining(), 30_000) }).toContain('TASK_STATE_FAILED');
  const history = await api.getMessages(sourceId);
  const text = history.messages.filter((row) => row.role === 'assistant').map(messageText).join('\n');
  expect(text).toContain(fast);
  const reported = history.messages.filter((row) => row.role === 'assistant' && messageText(row).includes(value));
  expect(reported).toHaveLength(1);
  expect(reported[0].metadata).toMatchObject({ origin: 'engine', outcome: 'completed', input_ids: [] });
  const calls = history.messages.flatMap(messageToolCalls);
  const watchers = calls.filter((call) => call.toolName === 'terminal').map((call) => JSON.stringify(call.input))
    .filter((command) => command.includes('task.py') && command.includes('watch') && command.includes(longId));
  expect(watchers.filter((command) => /timeout\s+2s/.test(command))).toHaveLength(1);
  expect(watchers.filter((command) => !/timeout\s+2s/.test(command)).length).toBeGreaterThanOrEqual(1);
  expect(calls.some((call) => call.toolName === 'a2a_call')).toBe(false);
  expect((await api.downloadFileText(targetId, `/workspace/${filename}`)).trim().split('\n')).toEqual([value]);
  for (const row of references) expect(sessionInputRows(String(row.session_id))).toHaveLength(1);
  expect(sessionInputRows(sourceId)).toHaveLength(1);
  await page.goto(appPath(`/sessions/${sourceId}`));
  await expect(page.getByTestId('assistant-message').filter({ hasText: value }).last()).toBeVisible();
  await page.reload();
  await expect(page.getByTestId('assistant-message').filter({ hasText: value }).last()).toBeVisible();
  evidence.final = { sourceId, targetId, value, watchers, references: agentTaskReferences(agentId),
    source: sessionResponseRows(sourceId), targets: references.map((row) => sessionResponseRows(String(row.session_id))), history };
});
