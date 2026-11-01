/** Read persisted Workflow history without executing a Workflow or contacting a model. */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { postgresTextRows, sqlLiteral } from '../fixtures/dbOracle';
import { POSTGRES_CONTAINER_HANDLE, requireServiceContainer } from '../fixtures/serviceContainer';
import { onPassOnly } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';

const sessionId = randomUUID();
const responseId = randomUUID();
const sql = (statement: string) => postgresTextRows(requireServiceContainer(POSTGRES_CONTAINER_HANDLE),
  process.env.ASTRABOX_E2E_POSTGRES_DB || 'astrabox', process.env.ASTRABOX_E2E_POSTGRES_USER || 'astrabox', statement);
onPassOnly(async () => { sql(`DELETE FROM sessions WHERE session_id=${sqlLiteral(sessionId)}`); });

test('mirrored Workflow Agents have stable history and navigation alongside ordinary Agents', async ({ page, request }, info) => {
  const api = new AstraApi(request);
  const user = await api.data<{ user_id: string }>('GET', '/user/current');
  const agents = await api.data<Array<{ agent_id: string }>>('GET', '/agents');
  const environment = process.env.ASTRABOX_E2E_ENVIRONMENT_NAME;
  expect(environment).toBeTruthy();
  expect(agents.length).toBeGreaterThan(0);
  const workflows = [
    { task: 'w-recorded-one', run: 'wf_recorded-one', name: 'Recorded Workflow one' },
    { task: 'w-recorded-two', run: 'wf_recorded-two', name: 'Recorded Workflow two' },
  ];
  // The reported cardinality is two Workflow containers, four internal Agents and
  // five ordinary Agents. Text is synthetic; no production business records are copied.
  const children = Array.from({ length: 9 }, (_, index) => ({
    id: `a-recorded-${index}`, name: `Recorded Agent ${index}`,
    run: index < 5 ? '' : workflows[index === 5 ? 0 : 1]!.run,
    parent: index === 8 ? 'a-recorded-6' : '',
  }));
  const nativeMessages = (id: string) => [
    { type: 'user', uuid: `${id}-user`, message: { role: 'user', content: [{ type: 'text', text: `${id} request` }] } },
    { type: 'assistant', uuid: `${id}-working`, message: { id: `${id}-working`, role: 'assistant', stop_reason: 'tool_use', content: [
      { type: 'thinking', thinking: `${id} reasoning`, signature: 'recorded-signature' },
      { type: 'text', text: `${id} before tool` },
      { type: 'tool_use', id: `${id}-tool`, name: 'Read', input: { file_path: `/recorded/${id}.txt` } },
    ] } },
    { type: 'user', uuid: `${id}-result`, message: { role: 'user', content: [
      { type: 'tool_result', tool_use_id: `${id}-tool`, content: `${id} tool result` },
    ] } },
    { type: 'assistant', uuid: `${id}-done`, message: { id: `${id}-done`, role: 'assistant', stop_reason: 'end_turn', content: [
      { type: 'text', text: `${id} final answer` },
    ] } },
  ];
  const main = workflows.map((workflow) => ({ type: 'user', uuid: randomUUID(),
    message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: `launch-${workflow.task}`, content: workflow.name }] },
    // WorkflowOutput from the pinned Claude Code 2.1.308 sdk-tools.d.ts.
    toolUseResult: { status: 'async_launched', taskType: 'local_workflow', taskId: workflow.task,
      runId: workflow.run, workflowName: workflow.name },
  }));
  const scopes = [{ subpath: '', entries: main }, ...children.map((child) => ({
    subpath: `subagents/${child.run ? `workflows/${child.run}/` : ''}agent-${child.id}`,
    entries: [{ type: 'agent_metadata', toolUseId: `launch-${child.id}`, parentAgentId: child.parent,
      spawnDepth: child.parent ? 2 : 1, description: child.name, agentType: 'general-purpose' }, ...nativeMessages(child.id)],
  }))];
  const lifecycle = (id: string, name: string, taskType: string) => ({ type: 'data-subagent', id: `recorded-close-${id}`,
    data: { kind: 'lifecycle', engineKind: 'claude_code', engineRef: id, event: 'closed',
      engineEvent: 'task_notification', engineStatus: 'completed', description: name, taskType, operations: [] } });
  const chunks = [
    { type: 'start', messageId: responseId },
    ...children.slice(0, 5).map((child) => lifecycle(child.id, child.name, 'local_agent')),
    ...workflows.map((workflow) => lifecycle(workflow.task, workflow.name, 'local_workflow')),
    { type: 'text-start', id: 'root' }, { type: 'text-delta', id: 'root', delta: 'Recorded Workflow history' },
    { type: 'text-end', id: 'root' }, { type: 'finish', finishReason: 'stop' },
  ];
  sql(`INSERT INTO sessions (session_id, environment_name, user_id, session_kind, workspace_kind, agent_id, engine_kind, title, state) `
    + `VALUES (${sqlLiteral(sessionId)}, ${sqlLiteral(environment!)}, ${sqlLiteral(user.user_id)}, 'agent_chat', 'agent', ${sqlLiteral(agents[0]!.agent_id)}, 'claude_code', 'Recorded Workflow history', 'READY'); `
    + `INSERT INTO session_responses (session_id, response_id, origin, status, outcome, ended_at, conversation_order) `
    + `VALUES (${sqlLiteral(sessionId)}, ${sqlLiteral(responseId)}, 'engine', 'ended', 'completed', now(), 1); `
    + `INSERT INTO session_output (session_id, seq, response_id, part) VALUES `
    + chunks.map((part, index) => `(${sqlLiteral(sessionId)}, ${index + 1}, ${sqlLiteral(responseId)}, ${sqlLiteral(JSON.stringify(part))})`).join(',') + '; '
    + scopes.map((scope) => `WITH scope AS (INSERT INTO transcript_scopes (platform_session_id, project_key, native_session_id, subpath, last_sequence) `
      + `VALUES (${sqlLiteral(sessionId)}, '-workspace', ${sqlLiteral(sessionId)}, ${sqlLiteral(scope.subpath)}, ${scope.entries.length}) RETURNING scope_id) `
      + `INSERT INTO transcript_entries (scope_id, seq, append_id, batch_index, entry_json) `
      + scope.entries.map((entry, index) => `SELECT scope_id, ${index + 1}, 'recorded', ${index}, ${sqlLiteral(JSON.stringify(entry))} FROM scope`).join(' UNION ALL ') + ';').join(' '));
  const snapshot = () => sql(`SELECT json_build_object('subpath', s.subpath, 'seq', e.seq, 'entry', e.entry_json) `
    + `FROM transcript_scopes s JOIN transcript_entries e ON e.scope_id=s.scope_id WHERE s.platform_session_id=${sqlLiteral(sessionId)} ORDER BY s.subpath, e.seq`);
  const before = snapshot();
  const listed = await api.listChildRuns(sessionId);
  expect(listed.child_runs).toHaveLength(11);
  const byName = new Map(listed.child_runs.map((child) => [child.description, child]));
  for (const child of children) {
    const entry = byName.get(child.name)!;
    expect(entry).toBeDefined();
    expect(entry.closed).toBe(true);
    expect(entry.active).toBe(false);
    if (child.run) {
      const parentName = child.parent ? children.find((row) => row.id === child.parent)!.name
        : workflows.find((workflow) => workflow.run === child.run)!.name;
      expect(entry.parent_child_run_id).toBe(byName.get(parentName)!.child_run_id);
      expect(entry.depth).toBe(child.parent ? 3 : 2);
    } else {
      expect(entry.parent_child_run_id).toBeFalsy();
      expect(entry.depth).toBe(1);
    }
  }
  const histories = await Promise.all(children.map(async (child) => {
    const history = await api.getChildRunMessages(sessionId, byName.get(child.name)!.child_run_id);
    expect(history.messages.map(({ role, content }) => ({ role, content }))).toEqual(
      nativeMessages(child.id).map((row) => ({ role: row.message.role, content: row.message.content })));
    return { child: child.name, ...history };
  }));
  await openSessionView(page, sessionId);
  await page.getByRole('tab', { name: /^Agents/ }).click();
  const panel = page.getByTestId('subagent-agents-panel');
  await expect(panel.getByTestId('subagent-agent-row')).toHaveCount(11);
  const drawer = page.getByTestId('subagent-transcript-drawer');
  for (const workflow of workflows) {
    await panel.getByTestId('subagent-agent-row').filter({ hasText: workflow.name }).click();
    await expect(drawer.getByTestId('subagent-task-type')).toHaveText('local_workflow');
    const directChildren = children.filter((child) => child.run === workflow.run && !child.parent);
    await expect(drawer.getByTestId('subagent-child-navigation').getByTestId('subagent-agent-row')).toHaveCount(directChildren.length);
    for (const child of children.filter((row) => row.run === workflow.run)) {
      const parentName = child.parent ? children.find((row) => row.id === child.parent)!.name : workflow.name;
      if (child !== children.find((row) => row.run === workflow.run)) {
        await panel.getByTestId('subagent-agent-row').filter({ hasText: parentName }).click();
      }
      await drawer.getByTestId('subagent-child-navigation').getByTestId('subagent-agent-row').filter({ hasText: child.name }).click();
      await expect(drawer).toContainText(`${child.id} before tool`);
      await expect(drawer).toContainText(`${child.id} final answer`);
      const tool = drawer.locator(`[data-tool-call-id="${child.id}-tool"]`);
      await tool.locator('[data-slot="collapsible-trigger"]').click();
      await expect(tool).toContainText(`${child.id} tool result`);
      await drawer.getByTestId('reasoning-part').getByRole('button').click();
      await expect(drawer).toContainText(`${child.id} reasoning`);
    }
  }
  await page.reload();
  await page.getByRole('tab', { name: /^Agents/ }).click();
  await expect(panel.getByTestId('subagent-agent-row')).toHaveCount(11);
  expect(await api.listChildRuns(sessionId)).toEqual(listed);
  const reloadedChild = children[8]!;
  await panel.getByTestId('subagent-agent-row').filter({ hasText: reloadedChild.name }).click();
  await expect(drawer).toContainText(`${reloadedChild.id} final answer`);
  expect((await api.getChildRunMessages(sessionId, byName.get(reloadedChild.name)!.child_run_id)).messages).toEqual(
    histories[8]!.messages);
  await panel.getByTestId('subagent-agent-row').filter({ hasText: children[0]!.name }).click();
  await expect(drawer).toContainText(`${children[0]!.id} final answer`);
  expect(snapshot()).toEqual(before);
  await info.attach('native-workflow-history', { body: JSON.stringify({ scopes, before, listed, histories }), contentType: 'application/json' });
  await page.screenshot({ path: info.outputPath('workflow-children.png'), fullPage: true });
});
