/** A real HTTP tool timeout must settle its native background Agent, live. */
import { expect, test } from '@playwright/test';

import { AstraApi, messageText, type ChildRunRecord } from '../fixtures/astraApi';
import { sessionEvents } from '../fixtures/dbOracle';
import {
  configureNativeMcpClientAgent, discoverNativeMcpTools, NATIVE_MCP_PYTHON, nativeRootEntries, object,
  serverLog, startNativeMcpServer, trackNativeMcpScene,
} from '../fixtures/nativeMcpServer';
import { openSessionView, sendPrompt } from '../fixtures/sessionPage';
import { requireSandboxHandle, sandboxExec } from '../fixtures/sandboxOps';
import { mirrorSseBodies } from '../fixtures/sseBodies';

const SERVER = 'e2e_timeout';
const TOOL = `mcp__${SERVER}__hang`;
const TIMEOUT_MS = 5_000;
const DELAY_SECONDS = 120;
const TIMEOUT_TEXT = `timed out after ${TIMEOUT_MS / 1000}s`;
const ENV = { MCP_TOOL_TIMEOUT: String(TIMEOUT_MS), MCP_CONNECTION_NONBLOCKING: '0' };
const scene = trackNativeMcpScene('native-mcp-timeout-scene');

function nativeParentBlocks(sessionId: string): Record<string, unknown>[] {
  return nativeRootEntries(sessionId).flatMap(({ entry }) => {
    if (entry.type !== 'assistant' || entry.isSidechain === true) return [];
    const content = object(entry.message).content;
    return Array.isArray(content) ? content.map(object) : [];
  });
}

function startedMessages(sessionId: string): Record<string, unknown>[] {
  return sessionEvents(sessionId)
    .filter((event) => event.event_type === 'engine.message')
    .map((event) => object(object(event.payload).message))
    .filter((message) => message.__sdk_type === 'TaskStartedMessage');
}

function agentChildren(rows: ChildRunRecord[]): ChildRunRecord[] {
  return rows.filter((row) => row.task_type !== 'local_bash');
}

test('native HTTP MCP timeout settles one background Agent and preserves its tool error live', async ({ page, request }) => {
  const api = new AstraApi(request);
  const runId = `${Date.now()}-${test.info().workerIndex}`;
  const childMarker = `MCP_TIMEOUT_CHILD_${runId}`;
  const parentMarker = `MCP_TIMEOUT_PARENT_${runId}`;

  // A separate conversation owns the listener for its entire sandbox lifetime,
  // and it is answering before the client Agent is configured.
  const started = await startNativeMcpServer(api, scene, { runId, tool: 'hang' });
  const configured = await configureNativeMcpClientAgent(api, scene, {
    runId, label: 'timeout', server: SERVER, env: ENV,
  });
  expect(object(object(object(configured.engine_options).sdk_options).env)).toMatchObject(ENV);
  const sessionId = scene.clientSession;
  await api.setPermissionMode(sessionId, 'bypassPermissions');

  // Reach the real exposed endpoint from the actual client sandbox before
  // asking a model to call it. Discovery never invokes the withheld tool.
  const discovery = await discoverNativeMcpTools(api, scene);
  expect(discovery.map((tool) => tool.name)).toEqual(['hang']);
  expect(await serverLog(api, scene), 'discovery must not execute the timeout tool').toEqual([]);

  await page.setViewportSize({ width: 1280, height: 720 });
  await mirrorSseBodies(page);
  await openSessionView(page, sessionId);
  await sendPrompt(page, sessionId, [
    `E2E native MCP timeout ${runId}. Follow this protocol exactly.`,
    'Launch exactly one Agent with run_in_background=true and subagent_type=general-purpose.',
    `That child must call ${TOOL} exactly once with {"delay_seconds":${DELAY_SECONDS}}.`,
    'The child must not retry this call and must not call any other tool.',
    `After that MCP call returns a result or error, the child final answer must contain ${childMarker}.`,
    'The parent must not wait for the child and must not call the MCP tool itself.',
    `Immediately after the Agent tool returns, the parent must reply with ${parentMarker}.`,
    `When the child's completion notification arrives later, report its final ${childMarker} marker.`,
  ].join('\n'));
  await expect(page.getByTestId('assistant-message').filter({ hasText: parentMarker })).toBeVisible();
  const status = page.getByTestId('run-view').locator('header').getByTestId('status-pill');
  await expect(status).toHaveText(/Running in background|后台任务运行中/, { timeout: 30_000 });
  await expect(page.getByTestId('composer-prompt')).toBeEnabled();
  const initialReplies = (await api.getMessages(sessionId)).messages.filter((message) => (
    message.role === 'assistant' && messageText(message).includes(parentMarker)
  ));
  expect(initialReplies, 'the foreground launch reply is already durable').toHaveLength(1);
  const initialReply = initialReplies[0];

  const opened = agentChildren(await api.waitForChildRuns(sessionId, (rows) => (
    agentChildren(rows).length === 1
  )));
  const childId = opened[0].child_run_id;
  let timeoutToolId = '';
  await expect.poll(async () => {
    const transcript = await api.getChildRunMessages(sessionId, childId);
    const blocks = transcript.messages.flatMap((message) => message.content);
    const calls = blocks.filter((block) => block.type === 'tool_use' && block.name === TOOL);
    const call = calls[0];
    timeoutToolId = String(call?.id || '');
    const errors = blocks.filter((block) => (
      block.type === 'tool_result' && block.tool_use_id === timeoutToolId
      && timeoutToolId !== '' && block.is_error === true
      && JSON.stringify(block.content).includes(TIMEOUT_TEXT)
    ));
    return {
      calls: calls.length,
      delay: call ? object(call.input).delay_seconds : null,
      all_tool_calls: blocks.filter((block) => block.type === 'tool_use').length,
      timeout_errors: errors.length,
      child_finals: transcript.messages.filter((message) => message.role === 'assistant'
        && message.content.some((block) => block.type === 'text' && String(block.text).includes(childMarker))).length,
    };
  }, { timeout: 45_000, message: 'the same native child must retain one real tool call, its exact timeout error and final answer' })
    .toEqual({ calls: 1, delay: DELAY_SECONDS, all_tool_calls: 1, timeout_errors: 1, child_finals: 1 });

  const completed = agentChildren(await api.waitForChildRuns(sessionId, (rows) => (
    agentChildren(rows).length === 1 && agentChildren(rows)[0].closed
  )));
  expect(completed).toHaveLength(1);
  expect(completed[0]).toMatchObject({ child_run_id: childId, closed: true, engine_status: 'completed' });
  const settled = await api.waitForSession(sessionId, (session) => !session.current_turn_id && session.state === 'READY');
  expect(settled.last_error || null).toBeNull();

  const log = await serverLog(api, scene);
  expect(log.filter((entry) => entry.phase === 'started')).toEqual([
    expect.objectContaining({ tool: 'hang', delay_seconds: DELAY_SECONDS }),
  ]);
  expect(log.filter((entry) => entry.phase === 'returned'), 'the server has not supplied the tool answer').toEqual([]);

  const launches = nativeParentBlocks(sessionId).filter((block) => (
    block.type === 'tool_use' && block.name === 'Agent' && object(block.input).run_in_background === true
  ));
  expect(launches).toHaveLength(1);
  const description = String(object(launches[0].input).description || '').trim();
  expect(description).not.toEqual('');
  const startedTasks = startedMessages(sessionId).filter((message) => message.tool_use_id === launches[0].id);
  expect(startedTasks).toHaveLength(1);
  expect(startedTasks[0].description).toBe(description);
  expect(completed[0].description).toBe(description);
  const completedTasks = sessionEvents(sessionId)
    .filter((event) => event.event_type === 'engine.message')
    .map((event) => object(object(event.payload).message))
    .filter((message) => message.task_id === startedTasks[0].task_id && (
      (message.__sdk_type === 'TaskNotificationMessage' && message.status === 'completed')
      || (message.__sdk_type === 'TaskUpdatedMessage' && object(message.patch).status === 'completed')
    ));
  // SDK 0.2.152 explicitly permits terminal TaskUpdated without notification;
  // both name the same task whose launch handle was checked above.
  expect(completedTasks.length, 'the exact launched task has a native terminal completion').toBeGreaterThan(0);
  expect(nativeParentBlocks(sessionId).filter((block) => block.type === 'tool_use' && block.name === TOOL))
    .toEqual([]);

  // The launch reply may quote the future child marker. Completion belongs
  // to its later turn, not to every bubble containing that substring.
  await expect.poll(async () => (await api.getMessages(sessionId)).messages.filter((message) => (
    message.role === 'assistant' && message.turn_id !== initialReply.turn_id
    && messageText(message).includes(childMarker)
  )).length, { timeout: 45_000, message: 'one subsequent parent turn reports the completed child' }).toBe(1);
  const history = await api.getMessages(sessionId);
  const completionReplies = history.messages.filter((message) => (
    message.role === 'assistant' && message.turn_id !== initialReply.turn_id
  ));
  expect(completionReplies, 'the completion produces one distinct parent reply').toHaveLength(1);
  expect(messageText(completionReplies[0])).toContain(childMarker);
  await expect(page.locator(`[data-message-id="${completionReplies[0].message_id}"]`)
    .getByTestId('assistant-message')).toHaveCount(1);
  await expect(page.locator(`[data-message-id="${completionReplies[0].message_id}"]`)
    .getByTestId('assistant-message')).toContainText(childMarker);
  await expect(page.getByTestId('assistant-message'))
    .toHaveCount(history.messages.filter((message) => message.role === 'assistant').length);
  const retainedInitial = history.messages.filter((message) => message.message_id === initialReply.message_id);
  expect(retainedInitial, 'the original parent reply retains one identity').toHaveLength(1);
  expect(messageText(retainedInitial[0]), 'a child completion must not append text to the earlier parent reply')
    .toBe(messageText(initialReply));
  const publicParentText = history.messages.filter((message) => message.role === 'assistant')
    .flatMap((message) => message.blocks || []).filter((block) => block.type === 'text')
    .map((block) => String(block.text || '')).join('\n');
  await expect.poll(() => nativeParentBlocks(sessionId).filter((block) => block.type === 'text')
    .map((block) => String(block.text || '')).join('\n'), {
    timeout: 30_000,
    message: 'public parent speech must equal the actual native replies, in order',
  }).toBe(publicParentText);
  const retained = await api.getChildRunMessages(sessionId, childId);
  expect(JSON.stringify(retained)).toContain(TIMEOUT_TEXT);
  expect(JSON.stringify(retained)).toContain(timeoutToolId);
  await page.getByRole('tab', { name: /^Agents/ }).click();
  await expect(page.getByTestId('subagent-agents-panel').getByTestId('subagent-agent-row')).toHaveCount(1);
  await expect(page.locator(`[data-testid="subagent-agent-row"][data-child-run-id="${childId}"]`))
    .toContainText('completed');
  await expect(status).toHaveText(/Ready|就绪/);

  // The terminal has its own PID namespace, separate from the Agent's. Read
  // the actual vendor process from the owning container, scoped to this
  // session's workload uid; emit only the two non-secret configured values.
  const detail = await api.adminSessionDetail(sessionId);
  const linuxUser = String(detail.runtime_identity?.linux_user || '').trim();
  expect(linuxUser, 'the client runtime names its workload account').toMatch(/^[a-z_][a-z0-9_-]*$/);
  expect(detail.runtime_identity?.sandbox_id).toBe(settled.sandbox_id);
  const sandbox = await requireSandboxHandle(api, String(settled.sandbox_id));
  const source = [
    'import json, os, pwd',
    'from pathlib import Path',
    `uid = pwd.getpwnam(${JSON.stringify(linuxUser)}).pw_uid`,
    `names = ${JSON.stringify(Object.keys(ENV))}`,
    'found = []',
    'for path in Path("/proc").iterdir():',
    '    if not path.name.isdigit(): continue',
    '    try:',
    '        if path.stat().st_uid != uid: continue',
    '        argv = (path / "cmdline").read_bytes().split(b"\\0")',
    '        if not any(os.path.basename(arg.decode(errors="replace")) in {"claude", "cli.js"} for arg in argv): continue',
    '        env = dict(item.split(b"=", 1) for item in (path / "environ").read_bytes().split(b"\\0") if b"=" in item)',
    '    except (FileNotFoundError, ProcessLookupError):',
    '        continue',
    '    selected = {name: env[name.encode()].decode() for name in names if name.encode() in env}',
    '    found.append({"pid": int(path.name), "env": selected})',
    'print(json.dumps(found))',
  ].join('\n');
  const effective: unknown = JSON.parse(sandboxExec(sandbox, `${NATIVE_MCP_PYTHON} - <<'PY'\n${source}\nPY`));
  expect(Array.isArray(effective), 'effective vendor env probe returns process records').toBe(true);
  const processes = effective as Array<{ pid: number; env: Record<string, string> }>;
  expect(processes.length, 'the actual Claude vendor process remains resident').toBeGreaterThan(0);
  for (const process of processes) expect(process.env).toEqual(ENV);
  test.info().annotations.push({ type: 'native_mcp_timeout', description: JSON.stringify({
    sessionId, serverSession: scene.serverSession, serverUrl: scene.serverUrl, started, childId,
    timeoutToolId, launchToolId: launches[0].id, taskId: startedTasks[0].task_id, log, effective,
  }) });
});
