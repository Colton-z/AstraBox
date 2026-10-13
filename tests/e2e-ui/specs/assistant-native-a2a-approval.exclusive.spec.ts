/** A user's single approval travels through Hermes into the original Codex request. */
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi, messageText, type MessagePage } from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { documentsByField, sessionDoc, sessionEvents, snapshotDoc } from '../fixtures/dbOracle';
import { engineProfileFor } from '../fixtures/engineProfile';
import { absoluteApiUrl, apiPath, appPath } from '../fixtures/env';
import { nativeCalls, nativeRows } from '../fixtures/nativeChildLifecycle';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

type Obj = Record<string, unknown>;
type Part = { text?: string; data?: Obj };
type Task = { id: string; contextId: string; status: { state: string; message?: { parts: Part[] } };
  metadata: { session_id: string; turn_id: string; command_id: string; input_id: string };
  artifacts?: Array<{ parts: Part[] }> };

test.describe.configure({ retries: 0, timeout: 180_000 });
const sessions = trackSessions();
let assistantId = '';
let agentId = '';
const evidence: Obj = {};
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  if (assistantId) await api.deleteAssistant(assistantId);
  if (agentId) await api.deleteAgent(agentId);
});
test.afterEach(async ({}, info) => {
  await info.attach('native-a2a-approval-evidence', {
    body: JSON.stringify({ ...evidence, assistantId, agentId, sessions }), contentType: 'application/json',
  });
});

function object(value: unknown): Obj {
  expect(value !== null && typeof value === 'object' && !Array.isArray(value)).toBe(true);
  return value as Obj;
}

function blocks(messages: MessagePage): Obj[] {
  return messages.messages.flatMap((message) => (message.blocks || []).map(object));
}

async function rpc(request: APIRequestContext, method: string, params: Obj, timeout: number) {
  const response = await request.post(apiPath(`/agents/${agentId}/a2a`), {
    headers: { 'A2A-Version': '1.0' }, timeout,
    data: { jsonrpc: '2.0', id: randomUUID(), method, params },
  });
  expect(response.ok(), await response.text()).toBe(true);
  return await response.json() as { result?: Task; error?: Obj };
}

async function getTask(request: APIRequestContext, id: string, timeout: number): Promise<Task> {
  const body = await rpc(request, 'GetTask', { id }, timeout);
  expect(body.error, JSON.stringify(body)).toBeUndefined();
  expect(body.result).toBeDefined();
  return body.result!;
}

function interaction(task: Task): Obj {
  const pending = task.status.message?.parts.find((part) => part.data?.interaction_id)?.data;
  expect(pending, JSON.stringify(task)).toBeDefined();
  return pending!;
}

// Negative probes use the real runtime binding, never a fabricated user. Mint
// exactly the capability the server places in that Assistant's MCP definition;
// keep it in process memory and out of Playwright request traces/attachments.
function agentsCapability(bindingId: string): string {
  const token = execFileSync('docker', [
    'exec', requireServiceContainer(SERVER_CONTAINER_HANDLE), 'python', '-c',
    'import sys; from astrabox.core.service.orchestrator.transcript_capability import mint_platform_mcp_capability; print(mint_platform_mcp_capability(sys.argv[1], "agents"))',
    bindingId,
  ], { encoding: 'utf8', timeout: 10_000, stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  if (!/^[a-f0-9]{64}$/.test(token)) throw new Error('server did not return a valid agents capability');
  return token;
}

async function rejectedMcp(bindingId: string, token: string, args: Obj, timeout: number): Promise<Obj> {
  const response = await fetch(absoluteApiUrl(
    `/platform-mcp/${encodeURIComponent(bindingId)}/agents/${token}/mcp`,
  ), {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: randomUUID(), method: 'tools/call',
      params: { name: 'answer_agent_task', arguments: args } }),
    signal: AbortSignal.timeout(timeout),
  });
  expect(response.ok, `signed MCP HTTP ${response.status}`).toBe(true);
  const body = await response.json() as { error?: Obj };
  expect(body.error, 'the malformed answer must be refused by the real MCP route').toBeDefined();
  return body.error!;
}

test('Hermes answers one Codex authorization through the signed A2A interaction bridge', async ({ request, page }, info) => {
  const deadline = Date.now() + 155_000;
  const remaining = () => {
    const left = deadline - Date.now();
    expect(left, 'the entire native approval journey shares 155 seconds').toBeGreaterThan(0);
    return left;
  };
  const api = new AstraApi(request);
  const marker = `A2A_APPROVAL_${randomUUID().replace(/-/g, '')}`;
  const path = `/workspace/${marker}.txt`;
  const command = `printf '%s' '${marker}' > '${path}'; printf '%s' '${marker}'`;
  const profile = engineProfileFor('codex');
  const base = await api.getAgent(profile.agent_id);
  const options = object(base.engine_options || {});
  agentId = (await api.createAgent({
    name: `__e2e_approval_codex_${marker}`, environment_name: profile.environment_name,
    model: profile.model, model_catalog: base.model_catalog, prepared_capacity: 0,
    engine_options: { ...options, config: { ...object(options.config || {}), approval_policy: 'on-request' } },
  })).agent_id;
  assistantId = (await api.createAssistant({
    display_name: `__e2e_approval_hermes_${marker}`, environment_name: await api.assistantEnvironmentName(),
  })).assistant_id;
  // Omit model_config_override: exercise the deployment's configured Assistant model.
  expect((await api.getAssistant(assistantId)).model_config_override ?? null).toBeNull();
  const sourceId = (await api.startAssistantConversation(assistantId)).session_id;
  sessions.push(sourceId);
  Object.assign(evidence, { marker, path, command, sourceId });
  info.annotations.push({ type: 'a2a_source_session', description: sourceId },
    { type: 'a2a_target_agent', description: agentId });
  await api.waitForSessionReady(sourceId, Math.min(90_000, remaining()));

  const first = await api.sendTurn(sourceId, [
    `First use native a2a_list to find configured AstraBox Agent ${agentId}.`,
    'Use its returned configured peer name verbatim in native a2a_call, exactly once.',
    'Do not run terminal, curl, HTTP clients, or the command yourself. Do not approve any request yet.',
    'Send this instruction to that Agent:',
    `Use Codex exec_command exactly once with cmd ${JSON.stringify(command)}, workdir /workspace,`,
    'sandbox_permissions require_escalated, and justification asking the owner to approve this one file write.',
    `After the tool runs, reply exactly ${marker}. Do not replace it with a different tool or an unprivileged attempt.`,
    'When the Agent requests permission, stop and report its Task ID, Interaction ID, context ID,',
    'and every declared decision option verbatim. Wait for my next message before answering the interaction.',
  ].join('\n'), remaining());
  evidence.first = first;
  expect(first.errorText, JSON.stringify(first)).toBeNull();
  expect(first.toolNames.filter((name) => name === 'a2a_call')).toHaveLength(1);
  expect(first.toolNames.some((name) => /(?:^|_)answer_agent_task$/.test(name))).toBe(false);
  expect(first.toolNames.some((name) => /terminal|exec_command/.test(name))).toBe(false);

  const references = () => documentsByField('agent_task_references', '$.agent_id', agentId);
  const tasks = references().filter((row) => row.kind === 'task');
  expect(tasks, JSON.stringify(references())).toHaveLength(1);
  const taskId = String(tasks[0]._id);
  const targetId = String(tasks[0].session_id);
  sessions.push(targetId);
  const before = await getTask(request, taskId, remaining());
  evidence.before = before;
  expect(before.status.state).toBe('TASK_STATE_INPUT_REQUIRED');
  const pending = interaction(before);
  expect(pending.presentation).toBe('decision');
  const interactionId = String(pending.interaction_id);
  const decisions = (pending.options as Obj[]).map((option) => String(option.id));
  expect(decisions).toContain('accept');
  expect(first.text).toContain(taskId);
  expect(first.text).toContain(interactionId);
  expect(first.text).toContain(before.contextId);
  for (const decision of decisions) expect(first.text).toContain(decision);
  const owner = sessionDoc(sourceId)!;
  expect(sessionDoc(targetId)).toMatchObject({ user_id: owner.user_id, org_id: owner.org_id,
    agent_id: agentId, source_type: 'a2a' });
  expect(tasks[0]).toMatchObject({ user_id: owner.user_id, org_id: owner.org_id });

  const calls = () => nativeRows(targetId).flatMap((row) => nativeCalls(JSON.parse(String(row.entry_json)), row.subpath))
    .filter((call) => call.name === 'exec_command' && call.input.cmd === command);
  await expect.poll(() => calls().length, { timeout: remaining(), intervals: [300] }).toBe(1);
  const nativeCall = calls()[0];
  evidence.nativeCall = nativeCall;
  expect(nativeCall.input).toMatchObject({ cmd: command, workdir: '/workspace', sandbox_permissions: 'require_escalated' });
  expect(String(nativeCall.input.justification || '').trim()).not.toBe('');
  expect(object(pending.raw_input).itemId).toBe(nativeCall.id);

  const assertStillPending = async () => {
    const task = await getTask(request, taskId, remaining());
    expect(task.status.state).toBe('TASK_STATE_INPUT_REQUIRED');
    expect(interaction(task).interaction_id).toBe(interactionId);
    expect(task.metadata).toMatchObject(before.metadata);
    const absent = await request.get(apiPath(`/sessions/${targetId}/files/download?path=${encodeURIComponent(path)}`),
      { timeout: Math.min(10_000, remaining()) });
    expect(absent.status(), 'the requested file must not exist until the native authorization is answered').toBe(404);
    expect(documentsByField('interaction_snapshots', '$.session_id', targetId)
      .find((row) => row.interaction_id === interactionId)?.interaction_state).toBe('OPEN');
  };
  await assertStillPending();
  const ordinary = await rpc(request, 'SendMessage', {
    message: { messageId: randomUUID(), contextId: before.contextId, role: 'ROLE_USER', parts: [{ text: 'Approved' }] },
    configuration: { returnImmediately: true },
  }, remaining());
  expect(ordinary.error, 'ordinary approval text must not bypass the existing interaction').toBeDefined();
  await assertStillPending();

  const bindings = documentsByField('platform_mcp_bindings', '$.assistant_id', assistantId)
    .filter((row) => row.scope_kind === 'assistant_workspace');
  expect(bindings).toHaveLength(1);
  expect(bindings[0].user_id).toBe(owner.user_id);
  const bindingId = String(bindings[0].deployment_id);
  const token = agentsCapability(bindingId);
  const wrongInteraction = await rejectedMcp(bindingId, token, { agent_id: agentId, task_id: taskId,
    interaction_id: randomUUID(), answer: { decision: 'accept' }, message_id: randomUUID() }, remaining());
  expect(String(wrongInteraction.message)).toContain('Interaction does not belong to this task');
  await assertStillPending();
  const wrongDecision = await rejectedMcp(bindingId, token, { agent_id: agentId, task_id: taskId,
    interaction_id: interactionId, answer: { decision: 'approve' }, message_id: randomUUID() }, remaining());
  expect(String(wrongDecision.message)).toContain('Agent operation refused');
  await assertStillPending();
  evidence.guards = { ordinary: ordinary.error, wrongInteraction, wrongDecision };

  const answerMessageId = randomUUID();
  evidence.answerMessageId = answerMessageId;
  const second = await api.sendTurn(sourceId, [
    '批准刚才那个命令，只批准这一次，不批准整个会话。',
    'Use the agents MCP get_agent_task to read that exact pending Task, then answer_agent_task',
    'to submit my one-time authorization with the supplier decision accept, not acceptForSession.',
    `Use the Task ID and Interaction ID you received, and message_id ${answerMessageId} verbatim.`,
    'Do not call a2a_call again, execute_code, terminal, curl or any HTTP client. After the answer tool returns,',
    'report the authorization receipt and Task ID. Do not wait or repeatedly poll for the Agent to finish.',
  ].join('\n'), remaining());
  evidence.second = second;
  expect(second.errorText, JSON.stringify(second)).toBeNull();
  expect(second.toolNames.filter((name) => /(?:^|_)get_agent_task$/.test(name)).length).toBeGreaterThanOrEqual(1);
  expect(second.toolNames.filter((name) => /(?:^|_)answer_agent_task$/.test(name))).toHaveLength(1);
  expect(second.toolNames.findIndex((name) => /(?:^|_)get_agent_task$/.test(name)))
    .toBeLessThan(second.toolNames.findIndex((name) => /(?:^|_)answer_agent_task$/.test(name)));
  expect(second.toolNames.some((name) => /a2a_call|execute_code|terminal|exec_command/.test(name))).toBe(false);
  expect(second.text).toContain(taskId);

  let after = before;
  await expect.poll(async () => {
    after = await getTask(request, taskId, remaining());
    if (['TASK_STATE_FAILED', 'TASK_STATE_CANCELED', 'TASK_STATE_REJECTED'].includes(after.status.state)) {
      throw new Error(`original target Task failed: ${JSON.stringify(after)}`);
    }
    return after.status.state;
  }, { timeout: remaining(), intervals: [300, 500] }).toBe('TASK_STATE_COMPLETED');
  evidence.after = after;
  expect(after.id).toBe(before.id);
  expect(after.contextId).toBe(before.contextId);
  expect(after.metadata).toMatchObject(before.metadata);
  expect(await api.downloadFileText(targetId, path, Math.min(10_000, remaining()))).toBe(marker);
  const answered = documentsByField('interaction_snapshots', '$.session_id', targetId)
    .find((row) => row.interaction_id === interactionId)!;
  expect(answered).toMatchObject({ interaction_state: 'ANSWERED', response: { decision: 'accept' },
    turn_id: before.metadata.turn_id });
  evidence.answered = answered;
  // Refused ordinary text can reserve a protocol id, but cannot create another execution.
  const admitted = sessionEvents(targetId).filter((row) => row.event_type === 'command.accepted'
    && object(row.payload).command_type === 'StartTurn');
  expect(admitted).toHaveLength(1);
  expect(object(admitted[0].payload).client_message_id).toBe(taskId);
  expect(sessionDoc(targetId)).toMatchObject({ user_id: owner.user_id, org_id: owner.org_id });
  expect(snapshotDoc(targetId)).toMatchObject({ last_turn_id: before.metadata.turn_id });

  const sourceMessages = await api.getMessages(sourceId, 50);
  const sourceBlocks = blocks(sourceMessages);
  const taskReads = sourceBlocks.filter((block) => block.type === 'tool_use'
    && /(?:^|_)get_agent_task$/.test(String(block.name)));
  expect(taskReads).toHaveLength([...first.toolNames, ...second.toolNames]
    .filter((name) => /(?:^|_)get_agent_task$/.test(name)).length);
  for (const read of taskReads) expect(read.input).toEqual({ agent_id: agentId, task_id: taskId });
  const answerCall = sourceBlocks.filter((block) => block.type === 'tool_use'
    && /(?:^|_)answer_agent_task$/.test(String(block.name)));
  expect(answerCall).toHaveLength(1);
  const answerArgs = object(answerCall[0].input);
  expect(answerArgs).toEqual({ agent_id: agentId, task_id: taskId,
    interaction_id: interactionId, answer: { decision: 'accept' }, message_id: answerMessageId });
  const answerReference = references().find((row) => row.kind === 'answer' && row.message_id === answerArgs.message_id);
  expect(answerReference).toMatchObject({ user_id: owner.user_id, org_id: owner.org_id, session_id: targetId });
  const answerResult = sourceBlocks.find((block) => block.type === 'tool_result' && block.tool_use_id === answerCall[0].id);
  expect(answerResult).toBeDefined();
  expect(answerResult?.is_error).not.toBe(true);
  expect(JSON.stringify(answerResult)).toContain(taskId);

  const nativeOutput = () => nativeRows(targetId).map((row) => JSON.parse(String(row.entry_json)) as Obj)
    .filter((entry) => entry.type === 'response_item').map((entry) => object(entry.payload))
    .find((payload) => payload.type === 'function_call_output' && payload.call_id === nativeCall.id);
  await expect.poll(() => Boolean(nativeOutput()), { timeout: remaining(), intervals: [300] }).toBe(true);
  expect(String(nativeOutput()!.output)).toContain(marker);
  const targetMessages = await api.getMessages(targetId, 50);
  const targetBlocks = blocks(targetMessages);
  const shownCall = targetBlocks.find((block) => block.type === 'tool_use' && block.id === nativeCall.id);
  expect(shownCall).toBeDefined();
  expect(shownCall?.name).toBe('commandExecution');
  const argv = JSON.parse(execFileSync('python3', ['-c',
    'import json,shlex,sys; print(json.dumps(shlex.split(sys.stdin.read())))'], {
    input: String(object(shownCall!.input).command), encoding: 'utf8', timeout: 5000,
  })) as string[];
  expect(argv.at(-1)).toBe(nativeCall.input.cmd);
  const shownResult = targetBlocks.find((block) => block.type === 'tool_result' && block.tool_use_id === nativeCall.id);
  expect(shownResult).toBeDefined();
  expect(shownResult?.is_error).not.toBe(true);
  expect(JSON.stringify(shownResult)).toContain(marker);
  expect(targetMessages.messages.filter((message) => message.role === 'assistant').map(messageText).join('\n')).toContain(marker);
  expect(after.artifacts?.flatMap((artifact) => artifact.parts).map((part) => part.text || '').join('\n')).toContain(marker);
  Object.assign(evidence, { sourceMessages, targetMessages, nativeOutput: nativeOutput() });

  await page.goto(appPath(`/sessions/${targetId}`), { waitUntil: 'domcontentloaded', timeout: remaining() });
  await expect(page.getByTestId('assistant-turn-process').first()).toBeAttached({ timeout: remaining() });
  await revealAssistantProcess(page);
  const card = page.getByTestId('run-view').locator(`[data-tool-call-id=${JSON.stringify(nativeCall.id)}]`).first();
  await expect(card).toBeVisible({ timeout: remaining() });
  await page.reload({ waitUntil: 'domcontentloaded', timeout: remaining() });
  await expect(page.getByTestId('assistant-turn-process').first()).toBeAttached({ timeout: remaining() });
  await revealAssistantProcess(page);
  await expect(card).toBeVisible({ timeout: remaining() });
  await card.locator('[data-slot="collapsible-trigger"]').first().click({ timeout: remaining() });
  await expect(card.getByRole('heading', { name: 'Result', exact: true }).locator('..'))
    .toContainText(marker, { timeout: remaining() });
  await expect(page.getByTestId('assistant-message').filter({ hasText: marker }).last()).toBeVisible({ timeout: remaining() });
  expect(blocks(await api.getMessages(targetId, 50))).toEqual(targetBlocks);
  remaining();
});
