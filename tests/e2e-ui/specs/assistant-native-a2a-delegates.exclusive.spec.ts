/** Hermes is the A2A client; HTTP and database reads only observe the delegated work. */
import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { apiPath } from '../fixtures/env';
import { agentTaskReferences, sessionRow } from '../fixtures/ingressRecords';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

type Task = {
  id: string;
  contextId: string;
  status: { state: string };
  history?: Array<{ role: string; parts: Array<{ text?: string }> }>;
  artifacts?: Array<{ parts: Array<{ text?: string }> }>;
  metadata: { session_id: string; session_url_path: string };
};

// A2A 1.0 JSON-RPC names/fields come from the pinned official SDK's transport
// and protobuf definitions. These observer requests never submit Agent work.
async function observe<T>(request: APIRequestContext, agentId: string,
  method: 'GetTask' | 'ListTasks', params: Record<string, unknown>): Promise<T> {
  const response = await request.post(apiPath(`/agents/${agentId}/a2a`), {
    headers: { 'A2A-Version': '1.0' },
    data: { jsonrpc: '2.0', id: randomUUID(), method, params },
  });
  expect(response.ok(), await response.text()).toBe(true);
  const body = await response.json();
  expect(body.error, JSON.stringify(body)).toBeUndefined();
  return body.result as T;
}

const sessions = trackSessions();
let assistantId = '';
let agentId = '';
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  if (assistantId) await api.deleteAssistant(assistantId);
  if (agentId) await api.deleteAgent(agentId);
});

test('Hermes native A2A delegates two context-linked turns and returns the target result', async ({ request }, info) => {
  const startedAt = Date.now();
  const remaining = () => {
    const budget = 155_000 - (Date.now() - startedAt);
    expect(budget, 'the two native exchanges must fit the fixed test budget').toBeGreaterThan(0);
    return budget;
  };
  const api = new AstraApi(request);
  const marker = `A2A_${randomUUID().replace(/-/g, '')}`;
  const suffix = '_continued';
  const filename = `${marker}.txt`;
  agentId = (await api.createColdTestAgent(`__e2e_a2a_${marker}`)).agent_id;
  const environment = await api.assistantEnvironmentName();
  assistantId = (await api.createAssistant({
    display_name: `__e2e_native_a2a_${marker}`,
    environment_name: environment,
    model_config_override: { model_name: await api.assistantModelName(environment) },
  })).assistant_id;
  const sourceId = (await api.startAssistantConversation(assistantId)).session_id;
  sessions.push(sourceId);
  info.annotations.push({ type: "a2a_target_agent", description: agentId },
    { type: "a2a_source_session", description: sourceId });
  await api.waitForSessionReady(sourceId, Math.min(remaining(), 90_000));

  const reply = await api.sendTurn(sourceId, [
    `Use your native a2a tools to delegate to configured AstraBox Agent ${agentId}.`,
    'Make exactly two sequential a2a_call calls. Do not write the file yourself or use terminal/curl as an A2A client.',
    `First call: ask that Agent to write exactly ${marker} (no newline) to ${filename} in its own workspace,`,
    'then reply with the written content and the platform conversation link.',
    `Second call: use the context_id returned by the first call. Tell that same Agent to append exactly ${suffix}`,
    '(no newline) to the file it just created, then report the complete file content and its platform conversation link.',
    'Return that final Agent result, its conversation link, and the A2A context id to me.',
  ].join('\n'), remaining());
  await info.attach("assistant-native-a2a-reply", {
    body: JSON.stringify(reply), contentType: "application/json",
  });
  expect(reply.errorText, JSON.stringify(reply)).toBeNull();
  expect(reply.toolNames.filter((name) => name === 'a2a_call')).toHaveLength(2);
  expect(reply.text).toContain(marker + suffix);

  // Fresh target Agent isolates these references from every other conversation.
  const references = agentTaskReferences(agentId);
  const tasks = references.filter((row) => row.kind === 'task');
  expect(tasks, JSON.stringify(references)).toHaveLength(2);
  const targetIds = [...new Set(tasks.map((row) => String(row.session_id || '')))];
  expect(targetIds).toHaveLength(1);
  const targetId = targetIds[0];
  expect(targetId).not.toBe('');
  expect(targetId).not.toBe(sourceId);
  sessions.push(targetId);
  const contexts = [...new Set(tasks.map((row) => String(row.context_id || '')))];
  expect(contexts).toHaveLength(1);
  expect(contexts[0]).not.toBe('');
  const source = sessionRow(sourceId);
  const target = sessionRow(targetId);
  expect(source?.user_id).toBeTruthy();
  expect(target).toMatchObject({ user_id: source?.user_id, agent_id: agentId, source_type: 'a2a' });
  expect(tasks.every((row) => row.user_id === source?.user_id)).toBe(true);
  expect(reply.text).toContain(targetId);
  expect(reply.text).toContain(contexts[0]);

  const observed = await Promise.all(tasks.map((row) => observe<Task>(request, agentId, 'GetTask', { id: row.reference_id })));
  for (const task of observed) {
    expect(task.contextId).toBe(contexts[0]);
    expect(task.status.state).toBe('TASK_STATE_COMPLETED');
    expect(task.metadata.session_id).toBe(targetId);
    expect(task.metadata.session_url_path).toContain(`/sessions/${targetId}`);
  }
  const listed = await observe<{ tasks: Task[] }>(request, agentId, 'ListTasks', {
    contextId: contexts[0], includeArtifacts: true, pageSize: 100,
  });
  expect(listed.tasks.map((task) => task.id).sort()).toEqual(tasks.map((row) => row.reference_id).sort());
  const texts = observed.map((task) => task.artifacts?.flatMap((artifact) => artifact.parts)
    .map((part) => part.text || '').join('\n') || '');
  expect(texts).toContainEqual(expect.stringContaining(marker + suffix));
  const targetMessages = await api.getMessages(targetId, 50);
  expect(targetMessages.messages.filter((row) => row.role === 'user')).toHaveLength(2);
  expect(targetMessages.messages.filter((row) => row.role === 'assistant').map(messageText).join('\n'))
    .toContain(marker + suffix);
  const detail = await api.adminSessionDetail(targetId);
  const workspace = String(detail.runtime_identity?.workspace_dir || '');
  expect(workspace).toBe('/workspace');
  expect(await api.downloadFileText(targetId, `${workspace}/${filename}`, Math.min(remaining(), 30_000)))
    .toBe(marker + suffix);
  await info.attach('assistant-native-a2a-evidence', {
    body: JSON.stringify({ sourceId, targetId, agentId, assistantId, references,
      tasks: observed, reply, targetMessages, workspace, filename }),
    contentType: 'application/json',
  });
});
