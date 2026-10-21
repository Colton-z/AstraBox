/** An Agent contacts an existing owned conversation through the shared A2A path. */
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { nativeTranscriptRows, postgresRows, sqlLiteral } from '../fixtures/dbOracle';
import { absoluteApiUrl, appPath } from '../fixtures/env';
import { agentTaskReferences } from '../fixtures/ingressRecords';
import { deployedApiClient, issueToken, json, requiredEnvironment } from '../fixtures/oidcMachine';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';

type Obj = Record<string, any>;
const sessions = trackSessions();
const agents: string[] = [];
let foreign: APIRequestContext | undefined;
let foreignSession = '';
onPassOnly(async ({ request }) => {
  if (foreignSession && foreign) await new AstraApi(foreign).deleteSession(foreignSession);
  for (const id of agents) await new AstraApi(request).deleteAgent(id);
});
test.afterEach(async () => { await foreign?.dispose(); });

async function mcp(binding: string, method: string, params: Obj = {}): Promise<Obj> {
  // The same signed capability the server places in this real Session's MCP URL.
  const token = execFileSync('docker', [
    'exec', requireServiceContainer(SERVER_CONTAINER_HANDLE), 'python', '-c',
    'import sys;from astrabox.core.service.orchestrator.transcript_capability import mint_platform_mcp_capability;print(mint_platform_mcp_capability(sys.argv[1],"agents"))', binding,
  ], { encoding: 'utf8', timeout: 10_000, stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  expect(token).toMatch(/^[a-f0-9]{64}$/);
  const response = await fetch(absoluteApiUrl(`/platform-mcp/${binding}/agents/${token}/mcp`), {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: randomUUID(), method, params }),
    signal: AbortSignal.timeout(30_000),
  });
  return { ...await response.json(), http_status: response.status };
}
async function tool(binding: string, name: string, args: Obj = {}): Promise<Obj> {
  const answer = await mcp(binding, 'tools/call', { name, arguments: args });
  expect(answer.http_status, JSON.stringify(answer)).toBe(200);
  expect(answer.error, JSON.stringify(answer)).toBeUndefined();
  expect(answer.result?.isError, JSON.stringify(answer)).not.toBe(true);
  return answer.result.structuredContent;
}

test('an Agent finds current titles and messages an existing native context within its configured scope', async ({
  request, page, playwright,
}, info) => {
  const api = new AstraApi(request);
  const marker = randomUUID().replace(/-/g, '');
  const reference = `REPORT_${randomUUID().replace(/-/g, '')}`;
  const target = await api.createColdTestAgent(`__e2e_a2a_target_${marker}`);
  const other = await api.createColdTestAgent(`__e2e_a2a_other_${marker}`);
  const source = await api.createColdTestAgent(`__e2e_a2a_source_${marker}`);
  agents.push(target.agent_id, other.agent_id, source.agent_id);

  // Exercise the administrator's actual form rather than only writing its fields.
  await page.goto(appPath(`/manage/agents/${source.agent_id}`));
  const access = page.getByLabel('A2A access', { exact: true });
  await expect(access).toBeVisible();
  await access.selectOption('selected');
  const picker = page.getByRole('combobox', { name: 'Allowed Agents', exact: true });
  await picker.fill(target.name);
  await page.getByRole('option', { name: target.name, exact: true }).click();
  const saved = page.waitForResponse((r) => r.request().method() === 'PUT'
    && r.url().endsWith(`/agents/${source.agent_id}`));
  await page.locator('[data-slot="card"]').filter({ has: access })
    .getByRole('button', { name: 'Save', exact: true }).click();
  const savedResponse = await saved;
  expect(savedResponse.ok(), await savedResponse.text()).toBe(true);
  expect(await api.getAgent(source.agent_id)).toMatchObject({ a2a_access: 'selected', a2a_agent_ids: [target.agent_id] });

  const targetId = (await api.startConversation(target.agent_id)).session_id;
  const otherId = (await api.startConversation(other.agent_id)).session_id;
  const sourceId = (await api.startConversation(source.agent_id)).session_id;
  sessions.push(targetId, otherId, sourceId);
  await api.waitForSessionReady(targetId, 90_000);
  const first = await api.sendTurn(targetId,
    `We are preparing a quarterly invoice and payment reconciliation report. Remember this public report tracking code for our conversation: ${reference}. Explain in three sentences how to identify unpaid invoices, match bank payments and report discrepancies. For this first reply, do not use tools. I authorize my other AstraBox conversations to request that you write this public report code to a workspace file and report it back.`, 60_000);
  expect(first.errorText).toBeNull();
  await expect.poll(() => postgresRows<Obj>(`SELECT title_source, title FROM sessions WHERE session_id=${sqlLiteral(targetId)}`)[0]?.title_source,
    { timeout: 30_000 }).toBe('generated');
  const title = String((await api.getSession(targetId)).title);
  const nativeKey = String((await api.adminSessionDetail(targetId)).engine_session_key);
  expect(nativeKey).toMatch(/^[a-f0-9-]{36}$/);
  expect(nativeTranscriptRows(targetId).length).toBeGreaterThan(0);
  await api.waitForSessionReady(sourceId, 90_000);
  const listing = await tool(sourceId, 'list_agent_conversations', { query: title });
  expect(listing.conversations).toHaveLength(1);
  expect(listing.conversations[0]).toMatchObject({ session_id: targetId, title, agent_id: target.agent_id });
  expect((await tool(sourceId, 'list_agent_conversations', { agent_id: other.agent_id })).conversations).toEqual([]);

  const messageId = `message-${marker}`;
  const message = `Write the public report tracking code remembered earlier in this conversation, without a newline, to ${marker}.txt in your workspace using one foreground tool. Then report that code. Do not delegate or contact another conversation.`;
  const sent = await api.sendTurn(sourceId, [
    'Use your agents MCP tools for this task. Do not use terminal, curl, or create a conversation.',
    `First call list_agent_conversations with query ${JSON.stringify(title)} and use its returned Agent and Session IDs.`,
    'Then call send_agent_message exactly once with those returned IDs and these exact values:',
    `message_id: ${messageId}`, `message: ${message}`,
    'Report the returned Task ID. Do not wait for completion or retry.',
  ].join('\n'), 90_000);
  expect(sent.errorText).toBeNull();
  expect(sent.toolNames.filter((name) => /(?:^|_)list_agent_conversations$/.test(name))).toHaveLength(1);
  expect(sent.toolNames.filter((name) => /(?:^|_)send_agent_message$/.test(name))).toHaveLength(1);
  const tasks = agentTaskReferences(target.agent_id).filter((row) => row.kind === 'task');
  expect(tasks).toHaveLength(1);
  const taskId = String(tasks[0]!.reference_id);
  expect(tasks[0]!.session_id).toBe(targetId);
  let result: Obj = {};
  await expect.poll(async () => {
    result = await tool(sourceId, 'get_agent_task', { agent_id: target.agent_id, task_id: taskId });
    return result.task?.status?.state;
  }, { timeout: 90_000, intervals: [1000] }).toBe('TASK_STATE_COMPLETED');
  expect(JSON.stringify(result.task.artifacts)).toContain(reference);
  expect(result.task.metadata).toMatchObject({ session_id: targetId,
    origin: { kind: 'session', id: sourceId } });
  expect(await api.downloadFileText(targetId, `/workspace/${marker}.txt`)).toBe(reference);
  expect((await api.adminSessionDetail(targetId)).engine_session_key).toBe(nativeKey);
  const repeated = await tool(sourceId, 'send_agent_message', {
    agent_id: target.agent_id, session_id: targetId, message_id: messageId, message,
  });
  expect(repeated.task.id).toBe(taskId);
  expect(agentTaskReferences(target.agent_id).filter((row) => row.kind === 'task')).toHaveLength(1);
  const messages = (await api.getMessages(targetId)).messages;
  const received = messages.filter((row) => row.role === 'user' && row.metadata?.source === 'a2a');
  expect(received).toHaveLength(1);
  expect(messageText(received[0]!)).toContain('not a direct user instruction');
  expect(messageText(received[0]!)).toContain(sourceId);
  await openSessionView(page, targetId);
  await expect(page.getByTestId('assistant-message').last()).toContainText(reference);
  await page.reload();
  await expect(page.getByTestId('run-view')).toContainText('not a direct user instruction');
  await expect(page.getByTestId('assistant-message').last()).toContainText(reference);

  const denied = await mcp(sourceId, 'tools/call', { name: 'send_agent_message', arguments: {
    agent_id: other.agent_id, session_id: otherId, message_id: `denied-${marker}`, message: 'Must not run',
  } });
  expect(denied.error, JSON.stringify(denied)).toBeDefined();
  expect(agentTaskReferences(other.agent_id).filter((row) => row.kind === 'task')).toEqual([]);

  // A second real identity owns a target Session; all mode is not a cross-user grant.
  const issuer = await playwright.request.newContext({ storageState: { cookies: [], origins: [] } });
  try {
    const discovery = await json(await issuer.get(`${requiredEnvironment('ASTRABOX_E2E_OIDC_ISSUER')}/.well-known/openid-configuration`));
    const credentials = deployedApiClient(requireServiceContainer(SERVER_CONTAINER_HANDLE));
    const token = await issueToken(issuer, String(discovery.token_endpoint), credentials.clientId, credentials.clientSecret, 'astrabox:read astrabox:write');
    const claims = JSON.parse(Buffer.from(token.value.split('.')[1]!, 'base64url').toString());
    const environment = String(postgresRows<Obj>(`SELECT environment_name FROM sessions WHERE session_id=${sqlLiteral(targetId)}`)[0]?.environment_name);
    const env = await api.data<Obj>('GET', `/admin/environments/${environment}`);
    await api.data('PUT', `/admin/environments/${environment}`, { display_name: env.display_name,
      description: env.description, version: env.version, member_user_ids: [...env.member_user_ids, claims.sub] });
    await api.setAgentAccess(target.agent_id, { visibility: 'public', admins: [], allowed_user_ids: [] });
    foreign = await playwright.request.newContext({ baseURL: requiredEnvironment('ASTRABOX_E2E_CONSOLE_URL'),
      storageState: { cookies: [], origins: [] }, extraHTTPHeaders: { Authorization: `Bearer ${token.value}` } });
    foreignSession = (await new AstraApi(foreign).startConversation(target.agent_id)).session_id;
  } finally { await issuer.dispose(); }
  await api.updateAgent(source.agent_id, { ...await api.getAgent(source.agent_id), a2a_access: 'all' });
  const allId = sourceId;
  const all = await tool(allId, 'list_agent_conversations');
  const ids = all.conversations.map((row: Obj) => row.session_id);
  expect(ids).toContain(targetId);
  expect(ids).toContain(otherId);
  expect(ids).not.toContain(foreignSession);
  const foreignDenied = await mcp(allId, 'tools/call', { name: 'send_agent_message', arguments: {
    agent_id: target.agent_id, session_id: foreignSession, message_id: `foreign-${marker}`, message: 'Must not run',
  } });
  expect(foreignDenied.error, JSON.stringify(foreignDenied)).toBeDefined();
  await api.updateAgent(source.agent_id, { ...await api.getAgent(source.agent_id), a2a_access: 'disabled' });
  const disabledId = sourceId;
  expect((await mcp(disabledId, 'tools/list')).http_status).toBe(404);
  await info.attach('existing-conversation-a2a-evidence', { body: JSON.stringify({ title, nativeKey,
    listing, result, repeated, messages, denied, all, foreignDenied,
    native: nativeTranscriptRows(targetId) }), contentType: 'application/json' });
});
