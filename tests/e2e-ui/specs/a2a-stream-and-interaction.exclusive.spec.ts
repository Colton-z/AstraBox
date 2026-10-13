/** A2A 1.0 streams observe durable native work; disconnecting is not cancellation. */
import { randomUUID } from 'node:crypto';
import { isDeepStrictEqual } from 'node:util';
import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { documentsByField, framesForTurn, sessionDoc, sessionEvents, snapshotDoc } from '../fixtures/dbOracle';
import { absoluteApiUrl, apiPath } from '../fixtures/env';
import { killSandbox, requireSandboxHandle, sandboxRunning, waitForSandboxStopped } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

type Part = { text?: string; data?: Record<string, unknown> };
type Status = { state: string; timestamp: string; message?: { taskId: string; contextId: string; parts: Part[] } };
type Task = { id: string; contextId: string; status: Status; history?: unknown[];
  metadata: { session_id: string; turn_id: string; input_id: string; command_id: string };
  artifacts?: Array<{ parts: Part[] }> };
type StreamEvent = { task?: Task;
  statusUpdate?: { taskId: string; contextId: string; status: Status };
  artifactUpdate?: { taskId: string; contextId: string; artifact: { parts: Part[] }; lastChunk?: boolean } };

const sessions = trackSessions();
let agentId = '';
onPassOnly(async ({ request }) => { if (agentId) await new AstraApi(request).deleteAgent(agentId); });
const streams: Array<{ events: StreamEvent[]; readonly ended: boolean; close: () => Promise<void> }> = [];
test.afterEach(async ({}, info) => {
  if (['failed', 'timedOut', 'interrupted'].includes(String(info.status))) {
    await info.attach('a2a-stream-failure', { contentType: 'application/json',
      body: JSON.stringify({ agentId, streams: streams.map(({ events, ended }) => ({ events, ended })) }) });
  }
  await Promise.all(streams.splice(0).map((stream) => stream.close()));
});

function budget() {
  const deadline = Date.now() + 155_000;
  return () => {
    const left = deadline - Date.now();
    expect(left, 'the complete A2A journey shares the fixed business budget').toBeGreaterThan(0);
    return left;
  };
}

async function rpc<T>(request: APIRequestContext, method: string, params: object, version = '1.0'): Promise<T> {
  const response = await request.post(apiPath(`/agents/${agentId}/a2a`), {
    headers: { 'A2A-Version': version },
    data: { jsonrpc: '2.0', id: randomUUID(), method, params },
  });
  expect(response.ok(), await response.text()).toBe(true);
  const body = await response.json();
  expect(body.error, JSON.stringify(body)).toBeUndefined();
  return body.result as T;
}

// SDK v1.2.2 jsonrpc_dispatcher maps StreamResponse to camelCase JSON-RPC
// result.task/statusUpdate/artifactUpdate inside ordinary SSE data frames.
async function stream(request: APIRequestContext, method: string, params: object, remaining: () => number) {
  const cookie = (await request.storageState()).cookies.find((item) => item.name === 'astrabox_session');
  expect(cookie, 'Node SSE uses the same signed owner cookie as the platform API').toBeDefined();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new Error('A2A business deadline exceeded')), remaining());
  timer.unref();
  const response = await fetch(absoluteApiUrl(`/agents/${agentId}/a2a`), {
    method: 'POST', signal: controller.signal,
    headers: { 'Content-Type': 'application/json', 'A2A-Version': '1.0', Cookie: `${cookie!.name}=${cookie!.value}` },
    body: JSON.stringify({ jsonrpc: '2.0', id: randomUUID(), method, params }),
  });
  expect(response.ok, `A2A SSE HTTP ${response.status}`).toBe(true);
  expect(response.headers.get('content-type')).toContain('text/event-stream');
  const events: StreamEvent[] = [];
  let ended = false;
  let closing = false;
  let failure: unknown;
  const reader = response.body!.getReader();
  const done = (async () => {
    const decoder = new TextDecoder();
    let buffer = '';
    try {
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        let boundary: RegExpExecArray | null;
        while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
          const frame = buffer.slice(0, boundary.index);
          buffer = buffer.slice(boundary.index + boundary[0].length);
          const data = frame.split(/\r?\n/).filter((line) => line.startsWith('data:'))
            .map((line) => line.slice(5).trimStart()).join('\n');
          if (!data) continue;
          const envelope = JSON.parse(data);
          if (envelope.error) throw new Error(JSON.stringify(envelope.error));
          expect(envelope.result).toBeTruthy();
          events.push(envelope.result as StreamEvent);
        }
      }
    } catch (error) { if (!closing) failure = error; }
    finally { ended = true; clearTimeout(timer); }
  })();
  const result = {
    events, get ended() { return ended; },
    check() { if (failure) throw failure; },
    async until(predicate: (event: StreamEvent) => boolean) {
      while (true) {
        result.check();
        const event = events.find(predicate);
        if (event) return event;
        if (ended) throw new Error(`A2A stream ended before expected event: ${JSON.stringify(events)}`);
        remaining();
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
    },
    async close() { closing = true; controller.abort(); await done; },
  };
  streams.push(result);
  return result;
}

function input(text: string, contextId?: string) {
  return { message: { messageId: randomUUID(), role: 'ROLE_USER', parts: [{ text }],
    ...(contextId ? { contextId } : {}) }, configuration: { historyLength: 0 } };
}

function nativeToolCalls(sessionId: string, name: string): Array<Record<string, unknown>> {
  return documentsByField('transcript_entries', '$.platform_session_id', sessionId)
    .map((row) => JSON.parse(String(row.entry_json)) as { message?: { content?: Array<Record<string, unknown>> } })
    .flatMap((entry) => Array.isArray(entry.message?.content) ? entry.message.content : [])
    .filter((block) => block.type === 'tool_use' && block.name === name);
}

function heldPrompt(marker: string): string {
  return [
    'Use Bash exactly once to execute this command synchronously; do not run it in the background:',
    `printf '%s' '${marker}' > '${marker}.started'; until test -f '${marker}.release'; do sleep 0.2; done; printf '%s' '${marker}' > '${marker}.txt'`,
    `Then reply with exactly ${marker}. All files must be in your workspace.`,
    'Use Bash timeout 120000 and run_in_background false. The client will provide the release file; do not create it yourself.',
  ].join('\n');
}

async function waitForStarted(api: AstraApi, sessionId: string, marker: string, remaining: () => number) {
  await api.waitForSession(sessionId, (session) => {
    if (['FAILED', 'TERMINATED', 'DELETED'].includes(String(session.state))) {
      throw new Error(`Session ended before file observation: ${session.state} ${session.last_error || ''}`);
    }
    return ['READY', 'PROCESSING'].includes(String(session.state)) && Boolean(session.sandbox_id)
      && !session.runtime_unavailable;
  }, remaining());
  await expect.poll(async () => {
    try { return await api.downloadFileText(sessionId, `/workspace/${marker}.started`, Math.min(10_000, remaining())); }
    catch (error) {
      if (/^Error: files\/download -> 404:/.test(String(error))) return '';
      throw error;
    }
  }, { timeout: remaining(), intervals: [500] }).toBe(marker);
}

async function finished(api: AstraApi, request: APIRequestContext, initial: Task, marker: string, remaining: () => number) {
  const task = await rpc<Task>(request, 'GetTask', { id: initial.id });
  expect(task).toMatchObject({ id: initial.id, contextId: initial.contextId,
    status: { state: 'TASK_STATE_COMPLETED' } });
  const sessionId = task.metadata.session_id;
  await api.waitForSession(sessionId, (row) => row.last_turn_id === task.metadata.turn_id
    && row.last_turn_status === 'COMPLETED' && !row.pending_interaction, remaining());
  const references = documentsByField('agent_task_references', '$.agent_id', agentId);
  expect(references.filter((row) => row.kind === 'task' && row._id === task.id)).toHaveLength(1);
  const journal = sessionEvents(sessionId);
  expect(journal.some((row) => row.event_type === 'turn.completed' && row.turn_id === task.metadata.turn_id)).toBe(true);
  expect(framesForTurn(task.metadata.turn_id).some((row) => {
    const payload = row.payload as { type?: string; data?: { inputId?: string } };
    return payload.type === 'data-input-consumed' && payload.data?.inputId === task.metadata.input_id;
  })).toBe(true);
  const messages = await api.getMessages(sessionId, 100);
  expect(messages.messages.filter((row) => row.role === 'assistant').map(messageText).join('\n')).toContain(marker);
  expect(task.artifacts?.flatMap((artifact) => artifact.parts).map((part) => part.text || '').join('\n')).toContain(marker);
  return { task, journal, references, messages };
}

test('an A2A task continues after its client disconnects and a subscription returns the real result', async ({ request }, info) => {
  const remaining = budget();
  const api = new AstraApi(request);
  const marker = `STREAM_${randomUUID().replaceAll('-', '')}`;
  const filename = `${marker}.txt`;
  const releaseFile = `${marker}.release`;
  agentId = (await api.createColdTestAgent(`__e2e_${marker}`)).agent_id;
  const initial = await stream(request, 'SendStreamingMessage', input(heldPrompt(marker)), remaining);
  const task = (await initial.until((event) => Boolean(event.task))).task!;
  sessions.push(task.metadata.session_id);
  expect(task.history ?? []).toHaveLength(0);
  expect(task.status.state).toBe('TASK_STATE_SUBMITTED');
  const coldSession = sessionDoc(task.metadata.session_id);
  expect(coldSession).toMatchObject({ state: 'CREATING', agent_id: agentId });
  const coldSnapshot = snapshotDoc(task.metadata.session_id);
  expect(coldSnapshot).toMatchObject({ current_turn_id: task.metadata.turn_id, delivery_state: 'PENDING' });
  const admission = sessionEvents(task.metadata.session_id).find((row) => row.event_type === 'command.accepted'
    && row.causation_id === `${task.metadata.session_id}:${task.id}`);
  expect(admission).toMatchObject({ turn_id: task.metadata.turn_id,
    payload: { command_type: 'StartTurn', content: heldPrompt(marker) } });
  await waitForStarted(api, task.metadata.session_id, marker, remaining);
  const nativeCalls = () => nativeToolCalls(task.metadata.session_id, 'Bash');
  await expect.poll(() => nativeCalls().length, { timeout: Math.min(15_000, remaining()) }).toBe(1);
  const nativeCall = nativeCalls()[0];
  await initial.until((event) => {
    const artifacts = event.task?.artifacts ?? (event.artifactUpdate ? [event.artifactUpdate.artifact] : []);
    return artifacts.flatMap((artifact) => artifact.parts).some((part) => {
      const blocks = part.data?.blocks;
      return Array.isArray(blocks) && blocks.some((block) => block.type === 'tool_use'
        && block.name === 'Bash' && block.id === nativeCall.id);
    });
  });
  expect(initial.events.some((event) => event.statusUpdate?.status.state === 'TASK_STATE_COMPLETED')).toBe(false);
  await initial.close();
  const resumed = await stream(request, 'SubscribeToTask', { id: task.id }, remaining);
  const resumedTask = (await resumed.until((event) => Boolean(event.task))).task!;
  expect(resumedTask).toMatchObject({ id: task.id, contextId: task.contextId });
  expect(resumedTask.status.state).toBe('TASK_STATE_WORKING');
  await api.uploadFileText(task.metadata.session_id, '/workspace', releaseFile, 'release', Math.min(10_000, remaining()));
  await resumed.until((event) => event.statusUpdate?.status.state === 'TASK_STATE_COMPLETED');
  expect(resumed.events.some((event) => event.artifactUpdate?.taskId === task.id)).toBe(true);
  const evidence = await finished(api, request, task, marker, remaining);
  expect(evidence.references.filter((row) => row.kind === 'task')).toHaveLength(1);
  const startup = evidence.journal.find((row) => row.event_type === 'session.startup_completed');
  expect(startup).toBeDefined();
  expect(Number(admission!.event_seq), 'durable input admission must precede cold startup completion')
    .toBeLessThan(Number(startup!.event_seq));
  const patchVersion = await rpc<Task>(request, 'GetTask', { id: task.id }, '1.0.7');
  expect(patchVersion).toMatchObject({ id: task.id, status: { state: 'TASK_STATE_COMPLETED' } });
  const nextMinor = await request.post(apiPath(`/agents/${agentId}/a2a`), {
    headers: { 'A2A-Version': '1.1' },
    data: { jsonrpc: '2.0', id: randomUUID(), method: 'GetTask', params: { id: task.id } },
  });
  expect((await nextMinor.json()).error?.code).toBe(-32009);
  expect(await api.downloadFileText(task.metadata.session_id, `/workspace/${filename}`, Math.min(30_000, remaining())))
    .toBe(marker);
  const listed = await rpc<{ tasks: Task[] }>(request, 'ListTasks', {
    statusTimestampAfter: evidence.task.status.timestamp, contextId: task.contextId,
  });
  expect(listed.tasks.map((item) => item.id)).toContain(task.id);
  for (const [method, params] of [
    ['SubscribeToTask', { id: task.id }],
    ['SendMessage', { message: { messageId: randomUUID(), role: 'ROLE_USER', taskId: task.id,
      parts: [{ data: { interaction_id: 'new-answer-on-terminal-task', answer: { decision: 'approve' } } }] } }],
  ] as const) {
    const refused = await request.post(apiPath(`/agents/${agentId}/a2a`), {
      headers: { 'A2A-Version': '1.0' }, data: { jsonrpc: '2.0', id: randomUUID(), method, params },
    });
    expect((await refused.json()).error?.code, method).toBe(-32004);
  }
  await info.attach('a2a-reconnected-stream', { contentType: 'application/json',
    body: JSON.stringify({ ...evidence, coldSession, coldSnapshot, admission, startup, initial: initial.events, resumed: resumed.events, filename }) });
});

test('an A2A input-required stream stays open and a structured answer completes its original task', async ({ request }, info) => {
  const remaining = budget();
  const api = new AstraApi(request);
  const marker = `ANSWER_${randomUUID().replaceAll('-', '')}`;
  agentId = (await api.createColdTestAgent(`__e2e_${marker}`)).agent_id;
  // The existing Agent API configures the native approval mode on this A2A context.
  const prepare = input('Reply with READY. Do not use tools.');
  const prepared = (await rpc<{ task: Task }>(request, 'SendMessage', {
    ...prepare, configuration: { ...prepare.configuration, acceptedOutputModes: ['text/plain'] },
  })).task;
  const preparedParts = prepared.artifacts?.flatMap((artifact) => artifact.parts) ?? [];
  expect(preparedParts.length).toBeGreaterThan(0);
  expect(preparedParts.every((part) => typeof part.text === 'string' && part.data === undefined)).toBe(true);
  expect(preparedParts.map((part) => part.text).join('\n')).toContain('READY');
  sessions.push(prepared.metadata.session_id);
  expect(prepared.status.state).toBe('TASK_STATE_COMPLETED');
  await api.setPermissionMode(prepared.metadata.session_id, 'default');
  const filename = `${marker}.txt`;
  const live = await stream(request, 'SendStreamingMessage', input([
    'Follow these steps in one turn, without finishing between tools.',
    `First call Write exactly once, relative file ${filename}, content exactly ${marker} with no newline.`,
    'Wait for permission and the actual Write result.',
    'Then call AskUserQuestion exactly once.',
    `Ask one single-select question containing ${marker}, header E2E, with YES and NO options.`,
    'Wait for my answer. Do not answer the question yourself.',
    `After YES, reply with exactly ${marker}. Use no tools other than Write and AskUserQuestion.`,
  ].join('\n'), prepared.contextId), remaining);
  const task = (await live.until((event) => Boolean(event.task))).task!;
  expect(task.history ?? []).toHaveLength(0);
  expect(task.id).not.toBe(prepared.id);
  expect(task.metadata.session_id).toBe(prepared.metadata.session_id);
  const authorization = await live.until((event) => (event.statusUpdate?.status ?? event.task?.status)?.state === 'TASK_STATE_AUTH_REQUIRED');
  expect((authorization.statusUpdate?.status ?? authorization.task!.status).message)
    .toMatchObject({ taskId: task.id, contextId: task.contextId });
  const approval = (authorization.statusUpdate?.status ?? authorization.task!.status).message!.parts
    .find((part) => part.data)?.data!;
  expect(approval).toMatchObject({ tool_name: 'Write', presentation: 'tool_approval' });
  await expect.poll(() => nativeToolCalls(task.metadata.session_id, 'Write').length,
    { timeout: Math.min(15_000, remaining()) }).toBe(1);
  const nativeWrite = nativeToolCalls(task.metadata.session_id, 'Write')[0];
  expect(nativeWrite.id).toBe(approval.tool_call_id);
  const follower = await stream(request, 'SubscribeToTask', { id: task.id }, remaining);
  const followingTask = (await follower.until((event) => Boolean(event.task))).task!;
  expect(followingTask).toMatchObject({ id: task.id, contextId: task.contextId,
    status: { state: 'TASK_STATE_AUTH_REQUIRED' } });
  const beforeApproval = live.events.length;
  await new Promise((resolve) => setTimeout(resolve, 1600));
  live.check(); follower.check();
  expect([live.ended, follower.ended], 'authorization interrupts both streams without closing them').toEqual([false, false]);
  const approved = await rpc<{ task: Task }>(request, 'SendMessage', {
    message: { messageId: randomUUID(), role: 'ROLE_USER', taskId: task.id, contextId: task.contextId,
      parts: [{ data: { interaction_id: approval.interaction_id, answer: { decision: 'approve' } } }] },
    configuration: { returnImmediately: true },
  });
  expect(approved.task.id).toBe(task.id);
  const waiting = await live.until((event) => (event.statusUpdate?.status ?? event.task?.status)?.state === 'TASK_STATE_INPUT_REQUIRED');
  await follower.until((event) => event.statusUpdate?.status.state === 'TASK_STATE_INPUT_REQUIRED');
  const status = waiting.statusUpdate?.status ?? waiting.task!.status;
  expect(status.message).toMatchObject({ taskId: task.id, contextId: task.contextId });
  const pending = status.message!.parts.find((part) => part.data)?.data!;
  expect(pending).toMatchObject({ tool_name: 'AskUserQuestion', presentation: 'form' });
  expect(JSON.stringify(pending)).toContain(marker);
  const turnId = String(pending.turn_id);
  const interactionId = String(pending.interaction_id);
  const nativeId = String(pending.tool_call_id);
  expect([pending.turn_id, pending.interaction_id, pending.tool_call_id].every((value) => typeof value === 'string' && value.length > 0)).toBe(true);
  expect(framesForTurn(turnId).some((row) => {
    const payload = row.payload as { type?: string; data?: Record<string, unknown> };
    return payload.type === 'data-interaction' && payload.data?.interaction_id === interactionId
      && payload.data?.tool_call_id === nativeId;
  })).toBe(true);
  await expect.poll(() => nativeToolCalls(task.metadata.session_id, 'AskUserQuestion').length,
    { timeout: Math.min(15_000, remaining()) }).toBe(1);
  const nativeQuestion = nativeToolCalls(task.metadata.session_id, 'AskUserQuestion')[0];
  expect(nativeQuestion.id).toBe(nativeId);
  // Keep the original connection across several server projection cycles before answering.
  await new Promise((resolve) => setTimeout(resolve, 1600));
  live.check(); follower.check();
  expect([live.ended, follower.ended], 'input-required is not terminal and must not close either subscription').toEqual([false, false]);
  expect(live.events.some((event) => event.artifactUpdate?.lastChunk === true),
    'the pending question must not mark its artifact final').toBe(false);
  const questions = pending.questions as Array<{ id: string }>;
  expect(questions).toHaveLength(1);
  const answer = { answers: [{ question_id: questions[0].id, option_label: 'YES' }] };
  const result = await rpc<{ task: Task }>(request, 'SendMessage', {
    message: { messageId: randomUUID(), role: 'ROLE_USER', taskId: task.id, contextId: task.contextId,
      parts: [{ data: { interaction_id: interactionId, answer } }] },
  });
  expect(result.task.id).toBe(task.id);
  await live.until((event) => event.statusUpdate?.status.state === 'TASK_STATE_COMPLETED');
  await follower.until((event) => event.statusUpdate?.status.state === 'TASK_STATE_COMPLETED');
  const updates = (events: StreamEvent[]) => events.filter((event) => event.statusUpdate || event.artifactUpdate);
  const common = live.events.slice(beforeApproval).find((event) => event.statusUpdate?.status.state === 'TASK_STATE_WORKING');
  expect(common, 'both streams must resume the same task after Write authorization').toBeDefined();
  const liveStart = live.events.findIndex((event, index) => index >= beforeApproval && isDeepStrictEqual(event, common));
  const followerStart = follower.events.findIndex((event) => isDeepStrictEqual(event, common));
  expect(followerStart, 'the full WORKING event anchors the same immutable journal boundary').toBeGreaterThanOrEqual(0);
  expect(updates(follower.events.slice(followerStart)), 'all updates from the common boundary through completion must have identical order')
    .toEqual(updates(live.events.slice(liveStart)));
  const evidence = await finished(api, request, task, marker, remaining);
  expect(evidence.task.metadata.turn_id).toBe(turnId);
  expect(approval.turn_id).toBe(turnId);
  expect(await api.downloadFileText(task.metadata.session_id, `/workspace/${filename}`, Math.min(15_000, remaining())))
    .toBe(marker);
  const interaction = documentsByField('interaction_snapshots', '$.session_id', task.metadata.session_id)
    .find((row) => row.interaction_id === interactionId);
  expect(interaction).toMatchObject({ interaction_state: 'ANSWERED', response: answer, turn_id: turnId });
  const approvalRecord = documentsByField('interaction_snapshots', '$.session_id', task.metadata.session_id)
    .find((row) => row.interaction_id === approval.interaction_id);
  expect(approvalRecord).toMatchObject({ interaction_state: 'ANSWERED', response: { decision: 'approve' },
    turn_id: turnId, tool_call_id: nativeWrite.id });
  expect(interaction).toMatchObject({ tool_call_id: nativeQuestion.id });
  const canonicalResponses = evidence.messages.messages.filter((row) => row.role === 'assistant' && row.turn_id === turnId);
  expect(canonicalResponses, 'this Task has one canonical response for its logical input').toHaveLength(1);
  const canonicalBlocks = canonicalResponses[0].blocks || [];
  const resultBlocks = (parts: Part[]) => parts.flatMap((part) => Array.isArray(part.data?.blocks)
    ? part.data.blocks as Array<Record<string, unknown>> : []);
  const finalArtifacts = [
    { surface: 'GetTask', blocks: resultBlocks(evidence.task.artifacts?.flatMap((artifact) => artifact.parts) ?? []) },
    ...[live, follower].map((connection, index) => {
      const finals = connection.events.filter((event) => event.artifactUpdate?.lastChunk === true);
      expect(finals.length, `SSE ${index + 1} must finalize its result artifact`).toBeGreaterThan(0);
      return { surface: `SSE ${index + 1}`, blocks: resultBlocks(finals.at(-1)!.artifactUpdate!.artifact.parts) };
    }),
  ];
  for (const { surface, blocks } of [{ surface: 'Session', blocks: canonicalBlocks }, ...finalArtifacts]) {
    // Required tool identities originate in native supplier custody, not in the A2A adapter.
    for (const tool of [nativeWrite, nativeQuestion]) {
      expect(blocks.filter((block) => block.type === 'tool_use' && block.name === tool.name && block.id === tool.id),
        `${surface}: native ${String(tool.name)} call`).toHaveLength(1);
      expect(blocks.filter((block) => block.type === 'tool_result' && block.tool_use_id === tool.id),
        `${surface}: matching ${String(tool.name)} result`).toHaveLength(1);
    }
    const questionResult = blocks.find((block) => block.type === 'tool_result' && block.tool_use_id === nativeQuestion.id)!;
    expect(JSON.stringify(questionResult.content), `${surface}: preserve the committed questionnaire answer`).toContain('YES');
    expect(blocks, `${surface}: complete canonical response blocks`).toEqual(canonicalBlocks);
  }
  expect(evidence.references.filter((row) => row.kind === 'task')).toHaveLength(2);
  await info.attach('a2a-structured-answer', { contentType: 'application/json',
    body: JSON.stringify({ ...evidence, interaction, approvalRecord, nativeWrite, nativeQuestion, finalArtifacts,
      events: live.events, follower: follower.events, approval, pending, answer }) });
});


test('confirmed compute loss fails the original A2A task and only a new input recovers its context', async ({ request }, info) => {
  const remaining = budget();
  const api = new AstraApi(request);
  const marker = `LOST_${randomUUID().replaceAll('-', '')}`;
  agentId = (await api.createColdTestAgent(`__e2e_${marker}`)).agent_id;
  const prompt = heldPrompt(marker);
  const live = await stream(request, 'SendStreamingMessage', input(prompt), remaining);
  const task = (await live.until((event) => Boolean(event.task))).task!;
  const sessionId = task.metadata.session_id;
  sessions.push(sessionId);
  expect(task.history ?? []).toHaveLength(0);
  await waitForStarted(api, sessionId, marker, remaining);
  await expect.poll(() => nativeToolCalls(sessionId, 'Bash').length,
    { timeout: Math.min(15_000, remaining()) }).toBe(1);
  const originalBash = nativeToolCalls(sessionId, 'Bash')[0];
  const before = await api.getSession(sessionId);
  const current = await rpc<Task>(request, 'GetTask', { id: task.id });
  expect(current.status.state).toBe('TASK_STATE_WORKING');
  const turnId = current.metadata.turn_id;
  const accepted = () => sessionEvents(sessionId).filter((row) => row.event_type === 'command.accepted'
    && (row.payload as Record<string, unknown>).command_type === 'StartTurn');
  const originalInputs = accepted();
  expect(originalInputs).toHaveLength(1);
  expect(originalInputs[0].payload).toMatchObject({ content: prompt });
  const sandbox = await requireSandboxHandle(api, String(before.sandbox_id));
  killSandbox(sandbox);
  await waitForSandboxStopped(sandbox, Math.min(30_000, remaining()));
  expect(sandboxRunning(sandbox)).toBe(false);
  await api.waitForSession(sessionId, (row) => row.last_turn_id === turnId
    && row.last_turn_status === 'FAILED' && !row.current_turn_id, remaining());
  const failedEvent = await live.until((event) => event.statusUpdate?.status.state === 'TASK_STATE_FAILED');
  await expect.poll(() => { live.check(); return live.ended; }, { timeout: Math.min(10_000, remaining()) }).toBe(true);
  const failed = await rpc<Task>(request, 'GetTask', { id: task.id });
  expect(failed).toMatchObject({ id: task.id, contextId: task.contextId,
    status: { state: 'TASK_STATE_FAILED', message: { taskId: task.id, contextId: task.contextId } } });
  expect(failedEvent.statusUpdate?.status.message).toMatchObject({ taskId: task.id, contextId: task.contextId });
  const terminal = sessionEvents(sessionId).find((row) => row.turn_id === turnId && row.event_type === 'turn.failed');
  expect(terminal).toBeDefined();
  const reason = String((terminal!.payload as Record<string, unknown>).error_text || '');
  expect(reason, 'the platform journal must retain the actual compute failure reason').not.toBe('');
  expect(failed.status.message!.parts.map((part) => part.text || '').join('\n')).toContain(reason);
  const history = await api.getMessages(sessionId, 100);
  expect(history.messages.filter((row) => row.role === 'user').map(messageText)).toEqual([prompt]);
  expect(accepted(), 'the dead task must not be automatically replayed').toEqual(originalInputs);
  expect(documentsByField('agent_task_references', '$.agent_id', agentId).filter((row) => row.kind === 'task')).toHaveLength(1);
  const nextMarker = `${marker}_NEW_INPUT`;
  const nextPrompt = `The previous task was interrupted by compute loss. Do not repeat it or run any tool. Reply with exactly ${nextMarker}.`;
  const nextInput = input(nextPrompt, task.contextId);
  const recovery = await stream(request, 'SendStreamingMessage', {
    ...nextInput, configuration: { ...nextInput.configuration, returnImmediately: true },
  }, remaining);
  const nextTask = (await recovery.until((event) => Boolean(event.task))).task!;
  expect(nextTask).toMatchObject({ contextId: task.contextId, status: { state: 'TASK_STATE_SUBMITTED' },
    metadata: { session_id: sessionId } });
  expect(nextTask.id).not.toBe(task.id);
  expect(nextTask.history ?? []).toHaveLength(0);
  const recoveringSession = sessionDoc(sessionId);
  expect(recoveringSession?.state === 'CREATING' || recoveringSession?.runtime_unavailable === true,
    'the new durable input must return while its runtime is still unavailable or starting').toBe(true);
  const recoveringSnapshot = snapshotDoc(sessionId);
  expect(recoveringSnapshot).toMatchObject({ current_turn_id: nextTask.metadata.turn_id, delivery_state: 'PENDING' });
  const nextAdmission = accepted().find((row) => row.causation_id === `${sessionId}:${nextTask.id}`);
  expect(nextAdmission).toMatchObject({ turn_id: nextTask.metadata.turn_id,
    payload: { command_type: 'StartTurn', content: nextPrompt } });
  await recovery.until((event) => event.statusUpdate?.status.state === 'TASK_STATE_COMPLETED');
  const recovered = await finished(api, request, nextTask, nextMarker, remaining);
  const nextStartup = recovered.journal.find((row) => row.event_type === 'session.startup_completed'
    && Number(row.event_seq) > Number(nextAdmission!.event_seq));
  expect(nextStartup, 'this input must precede the replacement runtime startup, not the original startup').toBeDefined();
  const rebound = await api.getSession(sessionId);
  expect(rebound.sandbox_id).toBeTruthy();
  expect(rebound.sandbox_id).not.toBe(sandbox.sandboxId);
  expect(await rpc<Task>(request, 'GetTask', { id: task.id })).toMatchObject({ id: task.id,
    status: { state: 'TASK_STATE_FAILED', message: { taskId: task.id, contextId: task.contextId } } });
  const inputsAfterRecovery = accepted();
  expect(inputsAfterRecovery).toHaveLength(2);
  expect(inputsAfterRecovery[0]).toEqual(originalInputs[0]);
  expect(inputsAfterRecovery[1].payload).toMatchObject({ content: nextPrompt });
  expect(nativeToolCalls(sessionId, 'Bash'), 'restoring native history must not re-execute the original held tool')
    .toEqual([originalBash]);
  expect(recovered.messages.messages.filter((row) => row.role === 'user').map(messageText)).toEqual([prompt, nextPrompt]);
  const taskReferences = recovered.references.filter((row) => row.kind === 'task');
  expect(taskReferences).toHaveLength(2);
  expect(taskReferences.find((row) => row._id === nextTask.id)).toMatchObject({ message_id: nextInput.message.messageId });
  expect(taskReferences.find((row) => row._id === task.id)?.message_id).not.toBe(nextInput.message.messageId);
  await info.attach('a2a-compute-loss', { contentType: 'application/json',
    body: JSON.stringify({ task, failed, sandbox, terminal, history, originalInputs, originalBash,
      events: live.events, nextTask, nextAdmission, nextStartup, recoveringSession, recoveringSnapshot,
      recovered, recoveryEvents: recovery.events }) });
});
