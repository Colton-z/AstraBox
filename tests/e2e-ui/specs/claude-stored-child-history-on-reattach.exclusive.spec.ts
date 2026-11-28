/** What a reattached Claude reader does with the child history already in the SessionStore. */
import { execFileSync, spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { postgresRows, postgresTextRows, sessionInputRows, sessionOutputRows, sessionResponseRows,
  sqlLiteral } from '../fixtures/dbOracle';
import { POSTGRES_CONTAINER_HANDLE, requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';

const sessions = trackSessions();
const server = () => requireServiceContainer(SERVER_CONTAINER_HANDLE);
const sql = (statement: string) => postgresTextRows(requireServiceContainer(POSTGRES_CONTAINER_HANDLE),
  process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox', process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox', statement);

function boundary(sid: string, action: 'arm' | 'read' | 'release' | 'clear') {
  const program = `
import json,os,pathlib,sys
sid,action=sys.argv[1:]
assert os.environ.get('ASTRABOX_E2E_FAULTS')=='1'
p=pathlib.Path(os.environ['ASTRABOX_E2E_FAULT_FILE']+'.d')/('stored-child-'+sid+'.json')
if action=='clear': p.unlink(missing_ok=True)
elif action in ('arm','release'):
 p.parent.mkdir(parents=True,exist_ok=True)
 value={'faults':{'hold_reader_boundary':1},'match':{'session_id':sid,'phase':'connected'},'release':False,'consumed':[]} if action=='arm' else {**json.loads(p.read_text()),'release':True}
 t=p.with_suffix('.tmp');t.write_text(json.dumps(value));t.chmod(0o644);t.replace(p)
print(p.read_text() if p.exists() else '{}')
`;
  return JSON.parse(execFileSync('docker', ['exec', server(), 'python', '-c', program, sid, action],
    { encoding: 'utf8', timeout: 10_000 }));
}

function serverLogsSince(since: string) {
  const logs = spawnSync('docker', ['logs', '--since', since, server()], { encoding: 'utf8', timeout: 30_000, maxBuffer: 64 << 20 });
  if (logs.error) throw logs.error;
  expect(logs.status, 'reading the server log must succeed').toBe(0);
  return `${logs.stdout}${logs.stderr}`;
}

type Scope = { subpath: string; entries: Array<Record<string, unknown>> };

/**
 * Answer one real turn, then hold the replacement reader that an eviction
 * starts, so the history written next and the input accepted next are what it
 * finds when it attaches.
 */
async function heldReattach(api: AstraApi, marker: string) {
  const agent = await api.defaultAgent();
  const sid = (await api.startConversation(agent.agent_id)).session_id;
  sessions.push(sid);
  await api.waitForSessionReady(sid, 90_000);
  const first = await api.postTurnInput(sid, `Reply with exactly FIRST_${marker} and nothing else. Do not use tools.`);
  const firstReply = await api.waitForReplyTo(sid, first.input_id, { timeoutMs: 60_000 });
  expect(firstReply.metadata?.outcome).toBe('completed');
  const projectKey = postgresRows<{ project_key: string }>(`SELECT project_key FROM transcript_scopes `
    + `WHERE platform_session_id=${sqlLiteral(sid)} AND subpath='' LIMIT 1`)[0]?.project_key;
  expect(projectKey).toBeTruthy();
  boundary(sid, 'arm');
  await api.adminEvictRuntime(sid);
  await expect.poll(() => boundary(sid, 'read').consumed.length, { timeout: 20_000 }).toBe(1);
  return { sid, projectKey: projectKey!, first, firstReply };
}

function writeScopes(sid: string, projectKey: string, nativeSession: string, marker: string, scopes: Scope[]) {
  sql(scopes.map((scope) => `WITH scope AS (INSERT INTO transcript_scopes (platform_session_id, project_key, native_session_id, subpath, last_sequence) `
    + `VALUES (${sqlLiteral(sid)}, ${sqlLiteral(projectKey)}, ${sqlLiteral(nativeSession)}, ${sqlLiteral(scope.subpath)}, ${scope.entries.length}) RETURNING scope_id) `
    + `INSERT INTO transcript_entries (scope_id, seq, append_id, batch_index, entry_json) `
    + scope.entries.map((entry, index) => `SELECT scope_id, ${index + 1}, 'stored-child-${marker}', ${index}, ${sqlLiteral(JSON.stringify(entry))} FROM scope`).join(' UNION ALL ') + ';').join(' '));
}

test.afterEach(async () => {
  for (const sid of sessions) boundary(sid, 'clear');
});

test('a resumed Workflow Agent split across its Workflow and plain transcripts keeps its history and the queued input is answered once', async ({ page, request }, info) => {
  const api = new AstraApi(request);
  const marker = randomUUID().replace(/-/g, '').slice(0, 12);
  const { sid, projectKey, first, firstReply } = await heldReattach(api, marker);

  // Claude Code 2.1.330's own shapes: a Workflow Agent's agent_metadata names no
  // spawning tool call, and a SendMessage resume racing the end of its original run
  // leaves the resumed request's reasoning in the Workflow path, the original
  // run's last record after it, and the resumed answer in the plain path.
  const agentId = `a${marker}`;
  const runId = `wf_${marker}`;
  const taskId = `w${marker}`;
  const workflowName = `Split Workflow ${marker}`;
  const nativeSession = randomUUID();
  const line = (uuid: string, parentUuid: string | null, body: Record<string, unknown>) => ({
    uuid: `${marker}-${uuid}`, parentUuid: parentUuid && `${marker}-${parentUuid}`, isSidechain: true, agentId,
    sessionId: nativeSession, ...body });
  const scopes: Scope[] = [
    { subpath: '', entries: [
      { type: 'assistant', uuid: `${marker}-launch`, parentUuid: null, sessionId: nativeSession, message: { id: `${marker}-m0`,
        role: 'assistant', stop_reason: 'tool_use', content: [{ type: 'tool_use', id: `${marker}-workflow`, name: 'Workflow', input: { name: workflowName } }] } },
      { type: 'user', uuid: `${marker}-launched`, parentUuid: `${marker}-launch`, sessionId: nativeSession, message: { role: 'user',
        content: [{ type: 'tool_result', tool_use_id: `${marker}-workflow`, content: workflowName }] },
        toolUseResult: { status: 'async_launched', taskType: 'local_workflow', taskId, runId, workflowName } },
      { type: 'assistant', uuid: `${marker}-send`, parentUuid: `${marker}-launched`, sessionId: nativeSession, message: { id: `${marker}-m1`,
        role: 'assistant', stop_reason: 'tool_use', content: [{ type: 'tool_use', id: `${marker}-sendmessage`, name: 'SendMessage',
          input: { to: agentId, message: `${marker} resumed request` } }] } },
      { type: 'user', uuid: `${marker}-sent`, parentUuid: `${marker}-send`, sessionId: nativeSession, message: { role: 'user',
        content: [{ type: 'tool_result', tool_use_id: `${marker}-sendmessage`, content: 'Message sent' }] },
        toolUseResult: { success: true, message: 'Message sent', resumedAgentId: agentId, revivedAgentId: agentId } },
    ] },
    { subpath: `subagents/workflows/${runId}/agent-${agentId}`, entries: [
      { type: 'agent_metadata', agentType: 'general-purpose', description: `${marker} Workflow Agent`, spawnDepth: 1, inWorkflowRun: true },
      line('o-user', null, { type: 'user', message: { role: 'user', content: `${marker} original request` } }),
      line('o-call', 'o-user', { type: 'assistant', requestId: `${marker}-req-o`, message: { id: `${marker}-msg-o`, role: 'assistant',
        stop_reason: 'tool_use', content: [{ type: 'tool_use', id: `${marker}-so`, name: 'StructuredOutput', input: { answer: `${marker} original answer` } }] } }),
      line('c-user', 'o-call', { type: 'user', message: { role: 'user', content: `${marker} resumed request` } }),
      line('c-think', 'c-user', { type: 'assistant', requestId: `${marker}-req-c`, message: { id: `${marker}-msg-c`, role: 'assistant',
        stop_reason: null, content: [{ type: 'thinking', thinking: `${marker} resumed reasoning`, signature: 'recorded' }] } }),
      line('o-result', 'o-call', { type: 'user', message: { role: 'user', content: [
        { type: 'tool_result', tool_use_id: `${marker}-so`, content: 'Structured output provided successfully' }] } }),
    ] },
    { subpath: `subagents/agent-${agentId}`, entries: [
      line('c-text', 'c-think', { type: 'assistant', requestId: `${marker}-req-c`, message: { id: `${marker}-msg-c`, role: 'assistant',
        stop_reason: 'end_turn', content: [{ type: 'text', text: `${marker} resumed answer` }] } }),
    ] },
  ];
  // The Workflow task reached this conversation's stream as an ended engine response.
  const workflowResponse = randomUUID();
  const parts = [
    { type: 'start', messageId: workflowResponse },
    { type: 'data-subagent', id: `stored-child-close-${taskId}`, data: { kind: 'lifecycle', engineKind: 'claude_code',
      engineRef: taskId, event: 'closed', engineEvent: 'task_notification', engineStatus: 'completed',
      description: workflowName, taskType: 'local_workflow', operations: [] } },
    { type: 'finish', finishReason: 'stop' },
  ];
  const lastSeq = Number(postgresRows<{ seq: number }>(`SELECT COALESCE(MAX(seq),0) AS seq FROM session_output `
    + `WHERE session_id=${sqlLiteral(sid)}`)[0]!.seq);
  sql(`INSERT INTO session_responses (session_id, response_id, origin, status, outcome, ended_at) `
    + `VALUES (${sqlLiteral(sid)}, ${sqlLiteral(workflowResponse)}, 'engine', 'ended', 'completed', now()); `
    + `INSERT INTO session_output (session_id, seq, response_id, part) VALUES `
    + parts.map((part, index) => `(${sqlLiteral(sid)}, ${lastSeq + index + 1}, ${sqlLiteral(workflowResponse)}, ${sqlLiteral(JSON.stringify(part))})`).join(',') + ';');
  writeScopes(sid, projectKey, nativeSession, marker, scopes);

  const since = new Date().toISOString();
  const second = await api.postTurnInput(sid, `Reply with exactly SECOND_${marker} and nothing else. Do not use tools.`);
  expect(sessionInputRows(sid).find((row) => row.input_id === second.input_id)?.state).toBe('pending');
  boundary(sid, 'release');
  const secondReply = await api.waitForReplyTo(sid, second.input_id, { timeoutMs: 60_000 });
  expect(secondReply.metadata?.outcome).toBe('completed');
  expect(messageText(secondReply)).toContain(`SECOND_${marker}`);
  const session = await api.getSession(sid);
  expect(session.state).toBe('READY');
  expect(session.recovery_reason ?? null).toBeNull();
  expect(sessionInputRows(sid).map((row) => ({ id: row.input_id, state: row.state, response: row.response_id }))).toEqual([
    { id: first.input_id, state: 'admitted', response: firstReply.id },
    { id: second.input_id, state: 'admitted', response: secondReply.id },
  ]);
  expect(sessionResponseRows(sid).map((row) => ({ id: row.response_id, inputs: row.input_ids }))).toEqual([
    { id: firstReply.id, inputs: [first.input_id] },
    { id: workflowResponse, inputs: [] },
    { id: secondReply.id, inputs: [second.input_id] },
  ]);
  expect(sessionOutputRows(sid, workflowResponse).map((row) => row.part)).toEqual(parts);
  const logs = serverLogsSince(since);
  expect(logs).not.toContain(`agent_id='${agentId}'`);
  expect(logs).not.toContain(`session ${sid} reader failed`);

  // One Agent: the Workflow path's records in native order, then the plain path's,
  // the resumed answer joining the reasoning of the same model message.
  const expected = [
    { role: 'user', content: [{ type: 'text', text: `${marker} original request` }] },
    { role: 'assistant', content: [{ type: 'tool_use', id: `${marker}-so`, name: 'StructuredOutput', input: { answer: `${marker} original answer` } }] },
    { role: 'user', content: [{ type: 'text', text: `${marker} resumed request` }] },
    { role: 'assistant', content: [{ type: 'thinking', thinking: `${marker} resumed reasoning`, signature: 'recorded' },
      { type: 'text', text: `${marker} resumed answer` }] },
    { role: 'user', content: [{ type: 'tool_result', tool_use_id: `${marker}-so`, content: 'Structured output provided successfully' }] },
  ];
  const listed = await api.listChildRuns(sid);
  const task = listed.child_runs.find((row) => row.description === workflowName);
  const child = listed.child_runs.find((row) => row.description === `${marker} Workflow Agent`);
  expect(task).toBeDefined();
  expect(child).toBeDefined();
  expect(child!.parent_child_run_id).toBe(task!.child_run_id);
  // The resumed answer's end_turn settles the Agent; the Workflow path alone ends on a tool call.
  expect(child!.closed).toBe(true);
  expect(child!.active).toBe(false);
  const history = await api.getChildRunMessages(sid, child!.child_run_id);
  expect(history.messages.map(({ role, content }) => ({ role, content }))).toEqual(expected);

  await openSessionView(page, sid);
  await expect(page.locator(`[data-message-id="${secondReply.id}"]`)).toContainText(`SECOND_${marker}`);
  await page.getByRole('tab', { name: /^Tasks/ }).click();
  const panel = page.getByTestId('session-tasks-panel');
  const drawer = page.getByTestId('subagent-transcript-drawer');
  await panel.getByTestId('subagent-agent-row').filter({ hasText: workflowName }).click();
  await drawer.getByTestId('subagent-child-navigation').getByTestId('subagent-agent-row')
    .filter({ hasText: `${marker} Workflow Agent` }).click();
  await expect(drawer).toContainText(`${marker} original request`);
  await expect(drawer).toContainText(`${marker} resumed request`);
  await expect(drawer).toContainText(`${marker} resumed answer`);
  await expect(drawer.locator(`[data-tool-call-id="${marker}-so"]`)).toHaveCount(1);
  await drawer.getByTestId('reasoning-part').getByRole('button').click();
  await expect(drawer).toContainText(`${marker} resumed reasoning`);
  await page.reload();
  await expect(page.locator(`[data-message-id="${secondReply.id}"]`)).toContainText(`SECOND_${marker}`);
  expect((await api.getChildRunMessages(sid, child!.child_run_id)).messages).toEqual(history.messages);
  await info.attach('split-workflow-reattach', { body: JSON.stringify({ sid, scopes, listed, history,
    inputs: sessionInputRows(sid), responses: sessionResponseRows(sid) }), contentType: 'application/json' });
});

test('child history outside the contract stops the reattached reader with a recorded fault instead of retrying', async ({ request }, info) => {
  const api = new AstraApi(request);
  const marker = randomUUID().replace(/-/g, '').slice(0, 12);
  const { sid, projectKey, first, firstReply } = await heldReattach(api, marker);
  // A plain-path Agent transcript with no agent_metadata and no Workflow transcript
  // of the same Agent: no reader can tell which tool call started it.
  const agentId = `b${marker}`;
  writeScopes(sid, projectKey, randomUUID(), marker, [{ subpath: `subagents/agent-${agentId}`, entries: [
    { type: 'assistant', uuid: `${marker}-orphan`, parentUuid: null, isSidechain: true, agentId,
      message: { id: `${marker}-msg`, role: 'assistant', stop_reason: 'end_turn', content: [{ type: 'text', text: `${marker} orphan` }] } },
  ] }]);

  const since = new Date().toISOString();
  const second = await api.postTurnInput(sid, `Reply with exactly SECOND_${marker} and nothing else. Do not use tools.`);
  boundary(sid, 'release');
  await expect.poll(async () => (await api.getSession(sid)).recovery_reason, { timeout: 30_000 })
    .toBe('runtime_protocol_incompatible');
  const session = await api.getSession(sid);
  expect(session.state).toBe('RECOVERY_REQUIRED');
  expect(session.last_error).toContain(`lacks agent_metadata agent_id='${agentId}'`);
  // The accepted input keeps its identity for the recovery that delivers it; nothing answered or failed it.
  const held = sessionInputRows(sid).find((row) => row.input_id === second.input_id);
  expect(held?.state).not.toBe('failed');
  expect(held?.response_id ?? null).toBeNull();
  expect(sessionResponseRows(sid).map((row) => row.response_id)).toEqual([firstReply.id]);
  expect(sessionInputRows(sid).find((row) => row.input_id === first.input_id)?.state).toBe('admitted');
  await expect(api.postTurnInput(sid, `THIRD_${marker}`)).rejects.toThrow(/-> 502: .*RUNTIME_PROTOCOL_INCOMPATIBLE/);
  // A fault that every reconnect reads again is recorded once, not retried.
  await new Promise((resolve) => setTimeout(resolve, 10_000));
  const logs = serverLogsSince(since);
  expect(logs.split(`session ${sid} runtime protocol incompatible`).length - 1).toBe(1);
  expect(logs).not.toContain(`session ${sid} reader failed`);
  await info.attach('recorded-projection-fault', { body: JSON.stringify({ sid, session,
    inputs: sessionInputRows(sid), responses: sessionResponseRows(sid) }), contentType: 'application/json' });
});
