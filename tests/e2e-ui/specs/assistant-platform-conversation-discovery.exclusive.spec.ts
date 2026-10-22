/** Natural discovery must distinguish platform Sessions from local Hermes recall. */
import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { postgresRows, sessionInputRows, sqlLiteral } from '../fixtures/dbOracle';
import { deployedApiClient, issueToken, json, requiredEnvironment } from '../fixtures/oidcMachine';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';
import { requireSandboxHandle, sandboxExec, type SandboxHandle } from '../fixtures/sandboxOps';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

type Obj = Record<string, any>;
const sessions = trackSessions();
let agentId = '';
let assistantId = '';
let foreign: APIRequestContext | undefined;
let foreignId = '';
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  if (foreign && foreignId) await new AstraApi(foreign).deleteSession(foreignId);
  if (assistantId) await api.deleteAssistant(assistantId);
  if (agentId) await api.deleteAgent(agentId);
});
test.afterEach(async () => { await foreign?.dispose(); });

function nativeRpc(handle: SandboxHandle, home: string, method: string, params: Obj): Obj {
  const program = `import asyncio,json,os,aiohttp
async def main():
 async with aiohttp.ClientSession() as client:
  async with client.ws_connect('http://127.0.0.1:9118/api/ws',headers={'X-AstraBox-Hermes-Token':os.environ['HERMES_DASHBOARD_SESSION_TOKEN']}) as ws:
   await ws.send_json({'jsonrpc':'2.0','id':'discovery-evidence','method':${JSON.stringify(method)},'params':json.loads(${JSON.stringify(JSON.stringify(params))})})
   while True:
    answer=await ws.receive_json(timeout=15)
    if answer.get('id')=='discovery-evidence':
     assert 'error' not in answer,answer
     print(json.dumps(answer['result']));return
asyncio.run(main())`;
  return JSON.parse(sandboxExec(handle, `source '${home}/astrabox-hermes.env'; "$HERMES_VENV/bin/python" - <<'PY'\n${program}\nPY`));
}

function resultPage(value: unknown): Obj | undefined {
  // The pinned MCP client wraps its text/structured payload in result; deferred
  // batches may wrap those same results in a results list.
  if (typeof value === 'string') {
    try { return resultPage(JSON.parse(value)); } catch { return undefined; }
  }
  if (!value || typeof value !== 'object') return undefined;
  const row = value as Obj;
  if (Array.isArray(row.conversations) && typeof row.has_more === 'boolean') return row;
  if (Array.isArray(row.results)) return row.results.map(resultPage).find(Boolean);
  return resultPage(row.structuredContent ?? row.result);
}

function discovery(events: Obj[]): { page: Obj; args: Obj } {
  for (const event of [...events].reverse()) {
    if (event.type !== 'tool.complete') continue;
    const p = event.payload;
    const calls = p.name === 'tool_call' ? (Array.isArray(p.args.calls) ? p.args.calls : [p.args]) : [{ name: p.name, arguments: p.args }];
    const call = calls.find((item: Obj) => String(item.name).endsWith('list_agent_conversations'));
    const page = call && resultPage(p.result);
    if (page) return { page, args: call.arguments };
  }
  throw new Error(`native completion has no executed platform discovery result: ${JSON.stringify(events)}`);
}

test('natural A2A discovery finds existing owned platform conversations when local exchange history is empty', async ({ request, playwright, page }, info) => {
  const api = new AstraApi(request);
  const marker = randomUUID().replace(/-/g, '');
  const agent = await api.createColdTestAgent(`__e2e_discovery_${marker}`);
  agentId = agent.agent_id;
  const owned: string[] = [];
  for (const topic of ['invoice reconciliation', 'release planning']) {
    const sid = (await api.startConversation(agentId)).session_id;
    sessions.push(sid);owned.push(sid);
    await api.waitForSessionReady(sid, 90_000);
    const answer = await api.sendTurn(sid, `Give one short sentence about ${topic}. No tools.`, 45_000);
    expect(answer.errorText).toBeNull();
  }
  await expect.poll(() => owned.every((sid) => postgresRows<Obj>(`SELECT title_source FROM sessions WHERE session_id=${sqlLiteral(sid)}`)[0]?.title_source === 'generated'), { timeout: 30_000 }).toBe(true);
  const titles = new Map<string, string>();
  for (const sid of owned) titles.set(sid, String((await api.getSession(sid)).title));

  // A real second identity owns a Session of the same visible Agent.
  const issuer = await playwright.request.newContext({ storageState: { cookies: [], origins: [] } });
  try {
    const metadata = await json(await issuer.get(`${requiredEnvironment('ASTRABOX_E2E_OIDC_ISSUER')}/.well-known/openid-configuration`));
    const credentials = deployedApiClient(requireServiceContainer(SERVER_CONTAINER_HANDLE));
    const token = await issueToken(issuer, String(metadata.token_endpoint), credentials.clientId, credentials.clientSecret, 'astrabox:read astrabox:write');
    const claims = JSON.parse(Buffer.from(token.value.split('.')[1]!, 'base64url').toString());
    const environment = String(postgresRows<Obj>(`SELECT environment_name FROM sessions WHERE session_id=${sqlLiteral(owned[0]!)}`)[0]!.environment_name);
    const env = await api.data<Obj>('GET', `/admin/environments/${environment}`);
    await api.data('PUT', `/admin/environments/${environment}`, { display_name: env.display_name, description: env.description,
      version: env.version, member_user_ids: [...env.member_user_ids, claims.sub] });
    await api.setAgentAccess(agentId, { visibility: 'public', admins: [], allowed_user_ids: [] });
    foreign = await playwright.request.newContext({ baseURL: requiredEnvironment('ASTRABOX_E2E_CONSOLE_URL'),
      storageState: { cookies: [], origins: [] }, extraHTTPHeaders: { Authorization: `Bearer ${token.value}` } });
    foreignId = (await new AstraApi(foreign).startConversation(agentId)).session_id;
  } finally { await issuer.dispose(); }

  const profile = await api.assistantEnvironmentName();
  assistantId = (await api.createAssistant({ display_name: `__e2e_discovery_source_${marker}`, runtime_profile_name: profile,
    model_config_override: { model_name: await api.assistantModelName(profile) } })).assistant_id;
  const source = (await api.startAssistantConversation(assistantId)).session_id;
  sessions.push(source);
  await api.waitForSessionReady(source, 90_000);
  const detail = await api.adminSessionDetail(source);
  const home = String(detail.runtime_identity?.config_dir);
  expect(home).toMatch(/^\/home\//);
  const handle = await requireSandboxHandle(api, String(detail.sandbox_id));
  const native = (nativeRpc(handle, home, 'session.active_list', {}).sessions as Obj[]).find((row) => row.session_key === detail.engine_session_key)!;
  expect(native).toBeTruthy();
  const localCount = () => Number(sandboxExec(handle, `find '${home}/a2a_conversations' -maxdepth 1 -name '*.jsonl' 2>/dev/null | wc -l`).trim());
  expect(localCount()).toBe(0);
  const agentSessions = () => postgresRows<Obj>('SELECT session_id FROM sessions WHERE agent_id IS NOT NULL AND hidden=false ORDER BY session_id');
  const before = agentSessions();
  const inputs = new Map([...owned, foreignId].map((sid) => [sid, sessionInputRows(sid)]));
  let cursor = 0;
  const nativeSince = () => {
    const ring = nativeRpc(handle, home, 'session.events.since', { session_id: native.id, last_seen: cursor });
    expect(ring.truncated).toBe(false);
    const events = ring.events as Obj[];
    if (events.length) cursor = Number(events.at(-1)!.seq);
    return events;
  };
  nativeSince();
  const evidence: Obj = { owned, titles: Object.fromEntries(titles), foreignId, source, localBefore: 0, turns: [] };
  try {
    const first = await api.sendTurn(source, '我有哪些对话？只查看，不要替我发消息或创建新的对话。', 45_000);
    expect(first.errorText).toBeNull();
    evidence.turns.push({ prompt: '我有哪些对话', answer: first.text, native: nativeSince() });
    const reply = await api.sendTurn(source, 'a2a的呢？先列1条已有的Agent对话，告诉我Agent名称、对话标题，以及是否还有下一页。仍然只查看。', 45_000);
    expect(reply.errorText).toBeNull();
    const secondEvents = nativeSince();
    const listing = discovery(secondEvents);
    expect(listing.args.limit).toBe(1);
    expect(listing.page.conversations).toHaveLength(1);
    expect(listing.page.has_more).toBe(true);
    const one = listing.page.conversations[0];
    expect(owned).toContain(one.session_id);
    expect(one.title).toBe(titles.get(one.session_id));
    expect(reply.text).toContain(one.title);
    expect(reply.text).toContain(agent.name);
    evidence.turns.push({ prompt: 'a2a的呢', answer: reply.text, native: secondEvents, listing });

    const next = await api.sendTurn(source, '继续下一页，还是只列1条，只查看。', 45_000);
    expect(next.errorText).toBeNull();
    const nextEvents = nativeSince();
    const another = discovery(nextEvents);
    expect(another.args.cursor).toBe(listing.page.next_cursor);
    expect(another.page.conversations).toHaveLength(1);
    expect(another.page.has_more).toBe(false);
    const two = another.page.conversations[0];
    expect(new Set([one.session_id, two.session_id])).toEqual(new Set(owned));
    expect(two.title).toBe(titles.get(two.session_id));
    expect(next.text).toContain(two.title);
    evidence.turns.push({ prompt: '下一页', answer: next.text, native: nextEvents, listing: another });

    const absent = `NO_MATCH_${marker}`;
    const empty = await api.sendTurn(source, `有没有标题包含 ${absent} 的已有Agent对话？只查询，不发消息、不建对话。`, 45_000);
    expect(empty.errorText).toBeNull();
    const emptyEvents = nativeSince();
    const none = discovery(emptyEvents);
    expect(none.args.query).toContain(absent);
    expect(none.page.conversations).toEqual([]);
    expect(none.page.has_more).toBe(false);
    expect(empty.text.trim()).not.toBe('');
    evidence.turns.push({ prompt: absent, answer: empty.text, native: emptyEvents, listing: none });
    expect(JSON.stringify(evidence.turns.map((turn: Obj) => turn.listing))).not.toContain(foreignId);
    expect(localCount()).toBe(0);
    expect(agentSessions()).toEqual(before);
    for (const sid of [...owned, foreignId]) expect(sessionInputRows(sid)).toEqual(inputs.get(sid));
    await openSessionView(page, source);
    await expect(page.getByTestId('run-view')).toContainText(one.title);
    await expect(page.getByTestId('run-view')).toContainText(two.title);
    await page.reload();
    await expect(page.getByTestId('run-view')).toContainText(two.title);
    evidence.history = await api.getMessages(source);
    evidence.localAfter = localCount();
    await info.attach('platform-conversation-discovery-cold', { body: await page.screenshot(), contentType: 'image/png' });
  } finally {
    await info.attach('platform-conversation-discovery-evidence', { body: JSON.stringify(evidence), contentType: 'application/json' });
  }
});
