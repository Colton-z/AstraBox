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

function nativeMessages(handle: SandboxHandle, home: string, key: string, after: number): Obj[] {
  const program = `import json,sqlite3
connection=sqlite3.connect(${JSON.stringify(`file:${home}/state.db?mode=ro`)},uri=True)
connection.row_factory=sqlite3.Row
rows=connection.execute("SELECT id,role,content,tool_calls,tool_call_id FROM messages WHERE session_id=? AND id>? ORDER BY id",(${JSON.stringify(key)},${after}))
print(json.dumps([dict(row) for row in rows]))`;
  return JSON.parse(sandboxExec(handle, `"$HERMES_VENV/bin/python" - <<'PY'\n${program}\nPY`));
}

function resultPage(value: unknown): Obj | undefined {
  // The pinned MCP client wraps its text/structured payload in result; deferred
  // batches may wrap those same results in a results list.
  if (typeof value === 'string') {
    // Hermes stores sufficiently long external results in its native
    // untrusted-data envelope before writing the tool message to SessionDB.
    const wrapped = value.match(/^<untrusted_tool_result source="[^"]+">\n[^\n]+\n\n([\s\S]*)\n<\/untrusted_tool_result>$/);
    try { return resultPage(JSON.parse(wrapped ? wrapped[1]! : value)); } catch { return undefined; }
  }
  if (!value || typeof value !== 'object') return undefined;
  const row = value as Obj;
  if (Array.isArray(row.conversations) && typeof row.has_more === 'boolean') return row;
  if (Array.isArray(row.results)) return row.results.map(resultPage).find(Boolean);
  return resultPage(row.structuredContent ?? row.result);
}

function discovery(messages: Obj[], matches: (args: Obj) => boolean = () => true): { page: Obj; args: Obj } {
  for (const message of [...messages].reverse()) {
    if (message.role !== 'assistant' || !message.tool_calls) continue;
    const outer = typeof message.tool_calls === 'string' ? JSON.parse(message.tool_calls) : message.tool_calls;
    for (const entry of outer) {
      const fn = entry.function;
      const args = typeof fn.arguments === 'string' ? JSON.parse(fn.arguments) : fn.arguments;
      const calls = fn.name === 'tool_call' ? args.calls : [{ name: fn.name, arguments: args }];
      const call = calls.find((item: Obj) => String(item.name).endsWith('list_agent_conversations') && matches(item.arguments));
      const result = messages.find((row) => row.role === 'tool' && row.tool_call_id === entry.id);
      const page = call && result && resultPage(result.content);
      if (page) return { page, args: call.arguments };
    }
  }
  throw new Error(`native SessionDB has no executed platform discovery result: ${JSON.stringify(messages)}`);
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
  const workspace = await api.waitForWorkspaceReady(assistantId, 90_000);
  const seedHandle = await requireSandboxHandle(api, String(workspace.current_sandbox_id));
  // The acceptance image passes this fixture transcript to native session.create;
  // it represents the old failed context, not tool executions in the new turn.
  const failedHistory = [
    { role: 'user', content: '我有哪些对话？只查看，不要替我发消息或创建新的对话。' },
    { role: 'assistant', content: null, tool_calls: [{ id: 'historical-local-search', type: 'function',
      function: { name: 'session_search', arguments: '{"limit":10}' } }] },
    { role: 'tool', tool_call_id: 'historical-local-search', name: 'session_search', content: '{"sessions":[]}' },
    { role: 'assistant', content: '没有找到任何已有对话。' },
    { role: 'user', content: 'a2a的呢？' },
    { role: 'assistant', content: null, tool_calls: [{ id: 'historical-local-a2a', type: 'function',
      function: { name: 'a2a_list', arguments: '{}' } }] },
    { role: 'tool', tool_call_id: 'historical-local-a2a', name: 'a2a_list',
      content: 'No persisted conversations. Metrics: 0 in / 0 out, 0 completed, 0 failed.' },
    { role: 'assistant', content: '全部为空，A2A侧没有任何对话记录。' },
  ];
  const seed = Buffer.from(JSON.stringify(failedHistory)).toString('base64');
  sandboxExec(seedHandle, `h=$(find /home/conversations -mindepth 2 -maxdepth 2 -name .hermes -type d | head -n 1)\n` +
    `printf '%s' '${seed}' | base64 -d > "$h/.astrabox-e2e-discovery-history.json"`);
  const source = (await api.startAssistantConversation(assistantId)).session_id;
  sessions.push(source);
  await api.waitForSessionReady(source, 90_000);
  const detail = await api.adminSessionDetail(source);
  const home = String(detail.runtime_identity?.config_dir);
  expect(home).toMatch(/^\/home\//);
  const handle = await requireSandboxHandle(api, String(detail.sandbox_id));
  const localCount = () => Number(sandboxExec(handle, `find '${home}/a2a_conversations' -maxdepth 1 -name '*.jsonl' 2>/dev/null | wc -l`).trim());
  expect(localCount()).toBe(0);
  const agentSessions = () => postgresRows<Obj>('SELECT session_id FROM sessions WHERE agent_id IS NOT NULL AND hidden=false ORDER BY session_id');
  const before = agentSessions();
  const inputs = new Map([...owned, foreignId].map((sid) => [sid, sessionInputRows(sid)]));
  const seededKey = postgresRows<Obj>(`SELECT engine_session_key FROM sessions WHERE session_id=${sqlLiteral(source)}`)[0]?.engine_session_key;
  expect(seededKey).toBeTruthy();
  const seeded = nativeMessages(handle, home, seededKey, 0);
  expect(seeded.filter((row) => row.role === 'assistant').map((row) => row.content)).toContain('全部为空，A2A侧没有任何对话记录。');
  let cursor = Number(seeded.at(-1)!.id);
  const nativeSince = () => {
    // Native drafts acquire their SessionDB key on the first submitted turn.
    // Read the current binding after that turn, not the earlier READY detail.
    const key = postgresRows<Obj>(`SELECT engine_session_key FROM sessions WHERE session_id=${sqlLiteral(source)}`)[0]?.engine_session_key;
    expect(key).toBeTruthy();
    // Replay is a bounded reconnect buffer (512 events), not a turn archive.
    // SessionDB retains the actual tool call and its matching supplier result.
    const events = nativeMessages(handle, home, key, cursor);
    if (events.length) cursor = Number(events.at(-1)!.id);
    return events;
  };
  const evidence: Obj = { owned, titles: Object.fromEntries(titles), foreignId, source, localBefore: 0, seeded, turns: [] };
  try {
    const first = await api.sendTurn(source, '我有哪些对话？只查看，不要替我发消息或创建新的对话。', 45_000);
    expect(first.errorText).toBeNull();
    const firstEvents = nativeSince();
    const firstListing = discovery(firstEvents);
    expect(firstListing.page.conversations.map((row: Obj) => row.session_id).sort()).toEqual([...owned].sort());
    expect(firstListing.page.has_more).toBe(false);
    for (const sid of owned) expect(first.text).toContain(titles.get(sid)!);
    evidence.turns.push({ prompt: '我有哪些对话', answer: first.text, native: firstEvents, listing: firstListing });
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

    // A title filter with no match is not the same as an owner with no
    // visible conversations. Archive only this fixture's two owned Sessions;
    // the other owner's Session stays visible to its own identity.
    for (const sid of owned) await api.archiveSession(sid);
    const owner = postgresRows<Obj>(`SELECT user_id FROM sessions WHERE session_id=${sqlLiteral(source)}`)[0]!.user_id;
    expect(postgresRows<Obj>(`SELECT session_id FROM sessions WHERE user_id=${sqlLiteral(owner)} AND agent_id IS NOT NULL AND hidden=false`)).toEqual([]);
    expect(postgresRows<Obj>(`SELECT hidden FROM sessions WHERE session_id=${sqlLiteral(foreignId)}`)[0]!.hidden).toBe(false);
    const emptyInventoryBefore = agentSessions();
    const noOwned = await api.sendTurn(source, '请重新查询我现在所有已有的Agent对话，不加任何标题或Agent筛选。只查看，不发消息、不建对话。', 45_000);
    expect(noOwned.errorText).toBeNull();
    const noOwnedEvents = nativeSince();
    const zero = discovery(noOwnedEvents, (args) =>
      ['query', 'agent_id', 'agent_query', 'cursor'].every((filter) => !args[filter]));
    for (const filter of ['query', 'agent_id', 'agent_query', 'cursor']) expect(zero.args[filter]).toBeFalsy();
    expect(zero.page.conversations).toEqual([]);
    expect(zero.page.has_more).toBe(false);
    expect(noOwned.text).toMatch(/没有|暂无|0\s*条|无.*对话/);
    expect(agentSessions()).toEqual(emptyInventoryBefore);
    for (const sid of [...owned, foreignId]) expect(sessionInputRows(sid)).toEqual(inputs.get(sid));
    evidence.turns.push({ prompt: '无筛选且用户没有可见Agent对话', answer: noOwned.text, native: noOwnedEvents, listing: zero });
    evidence.emptyOwner = { archived: owned, visibleOwned: 0, foreignStillVisibleToOwner: true };
    await page.reload();
    await expect(page.getByTestId('assistant-text').last()).toContainText(/没有|暂无|0\s*条|无.*对话/);
    evidence.history = await api.getMessages(source);
    evidence.localAfter = localCount();
    await info.attach('platform-conversation-discovery-cold', { body: await page.screenshot(), contentType: 'image/png' });
  } finally {
    await info.attach('platform-conversation-discovery-evidence', { body: JSON.stringify(evidence), contentType: 'application/json' });
  }
});
