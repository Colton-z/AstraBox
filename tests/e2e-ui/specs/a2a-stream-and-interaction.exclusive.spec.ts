/** A2A 1.0 streams observe durable native work; disconnecting is not cancellation. */
import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import {
  nativeTranscriptRows,
  sessionInputRows,
  sessionInteractionRows,
  sessionOutputRows,
  sessionResponseRows,
} from '../fixtures/dbOracle';
import { absoluteApiUrl, apiPath } from '../fixtures/env';
import { agentTaskReferences, sessionRow } from '../fixtures/ingressRecords';
import { killSandbox, requireSandboxHandle, sandboxRunning, waitForSandboxStopped } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

type Part = { text?: string; data?: Record<string, unknown> };
type Status = { state: string; timestamp: string; message?: { taskId: string; contextId: string; parts: Part[] } };
/**
 * A task as the A2A service projects it (`agent_a2a_service.py`): its id is
 * its input's `client_message_id` in the conversation, and its metadata names
 * that input and the response that answers it.
 */
type Task = { id: string; contextId: string; status: Status; history?: unknown[];
  metadata: { agent_id: string; session_id: string; input_id: string; response_id: string; session_url_path: string };
  artifacts?: Array<{ parts: Part[] }> };
type StreamEvent = { task?: Task;
  statusUpdate?: { taskId: string; contextId: string; status: Status };
  artifactUpdate?: { taskId: string; contextId: string; artifact: { parts: Part[] }; lastChunk?: boolean } };
/** One AI SDK UI message part, as the conversation's page folds a response. */
type UiPart = Record<string, unknown>;
type NativeCall = { id: string; name: string; input?: unknown };

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

/**
 * The conversation's own calls of one tool, as Claude Code wrote them into its
 * SessionStore (the main transcript, not a sub-agent's). A call is named once
 * by its native id: a resumed runtime may append a record of it again, which
 * is not a second execution.
 */
function nativeToolCalls(sessionId: string, name: string): NativeCall[] {
  const calls = new Map<string, NativeCall>();
  for (const row of nativeTranscriptRows(sessionId)) {
    if (row.subpath !== null) continue;
    const entry = JSON.parse(row.entry_json) as { message?: { content?: unknown } };
    const blocks = Array.isArray(entry.message?.content) ? entry.message.content as Array<Record<string, unknown>> : [];
    for (const block of blocks) {
      if (block.type !== 'tool_use' || block.name !== name) continue;
      const id = String(block.id || '');
      if (id && !calls.has(id)) calls.set(id, { id, name: String(block.name), input: block.input });
    }
  }
  return [...calls.values()];
}

/** The UI parts an A2A result carries: its data part holds the response as the conversation's page folds it. */
function resultParts(parts: Part[]): UiPart[] {
  return parts.flatMap((part) => (Array.isArray(part.data?.parts) ? part.data.parts as UiPart[] : []));
}

/** The tool parts showing one native call: same native id and tool name. */
function toolParts(parts: UiPart[], call: NativeCall): UiPart[] {
  return parts.filter((part) => part.toolCallId === call.id
    && (part.type === 'dynamic-tool' ? part.toolName === call.name : part.type === `tool-${call.name}`));
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

/**
 * The completed task, checked against the conversation's own records: the
 * session settles on the response the task names, that response answers the
 * task's input, and the task is one durable reference.
 */
async function finished(api: AstraApi, request: APIRequestContext, initial: Task, marker: string, remaining: () => number) {
  const task = await rpc<Task>(request, 'GetTask', { id: initial.id });
  expect(task).toMatchObject({ id: initial.id, contextId: initial.contextId,
    status: { state: 'TASK_STATE_COMPLETED' } });
  const sessionId = task.metadata.session_id;
  const responseId = task.metadata.response_id;
  expect(responseId, 'a completed task names the response that answered it').not.toBe('');
  const settled = await api.waitForSession(sessionId, (row) => row.state === 'READY'
    && row.last_response?.response_id === responseId, remaining());
  expect(settled.last_response?.outcome).toBe('completed');
  const references = agentTaskReferences(agentId);
  expect(references.filter((row) => row.kind === 'task' && row.reference_id === task.id)).toHaveLength(1);
  const inputs = sessionInputRows(sessionId);
  expect(inputs.filter((row) => row.client_message_id === task.id), 'the task is one input of its conversation')
    .toEqual([expect.objectContaining({ input_id: task.metadata.input_id, source: 'a2a', state: 'admitted',
      response_id: responseId })]);
  const responses = sessionResponseRows(sessionId);
  expect(responses.filter((row) => row.response_id === responseId), 'one response answers the task input')
    .toEqual([expect.objectContaining({ origin: 'input', status: 'ended', outcome: 'completed',
      input_ids: [task.metadata.input_id] })]);
  const messages = await api.getMessages(sessionId, 50);
  expect(messages.messages.filter((row) => row.role === 'assistant').map(messageText).join('\n')).toContain(marker);
  expect(task.artifacts?.flatMap((artifact) => artifact.parts).map((part) => part.text || '').join('\n')).toContain(marker);
  return { task, references, inputs, responses, messages };
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
  // The task is durable before compute exists: the conversation is still
  // starting and already holds the task's input, waiting for its runtime.
  const coldSession = sessionRow(task.metadata.session_id);
  expect(coldSession).toMatchObject({ state: 'CREATING', agent_id: agentId, source_type: 'a2a' });
  const admission = sessionInputRows(task.metadata.session_id);
  expect(admission).toEqual([expect.objectContaining({ input_id: task.metadata.input_id, client_message_id: task.id,
    source: 'a2a', content: heldPrompt(marker), state: 'pending' })]);
  await waitForStarted(api, task.metadata.session_id, marker, remaining);
  const nativeCalls = () => nativeToolCalls(task.metadata.session_id, 'Bash');
  await expect.poll(() => nativeCalls().length, { timeout: Math.min(15_000, remaining()) }).toBe(1);
  const nativeCall = nativeCalls()[0];
  await initial.until((event) => {
    const artifacts = event.task?.artifacts ?? (event.artifactUpdate ? [event.artifactUpdate.artifact] : []);
    return toolParts(resultParts(artifacts.flatMap((artifact) => artifact.parts)), nativeCall).length > 0;
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
  expect(evidence.inputs, 'disconnecting the first stream neither cancelled nor resent the task').toHaveLength(1);
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
    body: JSON.stringify({ ...evidence, coldSession, admission, nativeCall, initial: initial.events,
      resumed: resumed.events, filename }) });
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
  const sessionId = task.metadata.session_id;
  const authorization = await live.until((event) => (event.statusUpdate?.status ?? event.task?.status)?.state === 'TASK_STATE_AUTH_REQUIRED');
  expect((authorization.statusUpdate?.status ?? authorization.task!.status).message)
    .toMatchObject({ taskId: task.id, contextId: task.contextId });
  const approval = (authorization.statusUpdate?.status ?? authorization.task!.status).message!.parts
    .find((part) => part.data)?.data!;
  expect(approval).toMatchObject({ tool_name: 'Write', presentation: 'tool_approval' });
  await expect.poll(() => nativeToolCalls(sessionId, 'Write').length,
    { timeout: Math.min(15_000, remaining()) }).toBe(1);
  const nativeWrite = nativeToolCalls(sessionId, 'Write')[0];
  expect(nativeWrite.id).toBe(approval.tool_call_id);
  const follower = await stream(request, 'SubscribeToTask', { id: task.id }, remaining);
  const followingTask = (await follower.until((event) => Boolean(event.task))).task!;
  expect(followingTask).toMatchObject({ id: task.id, contextId: task.contextId,
    status: { state: 'TASK_STATE_AUTH_REQUIRED' } });
  const beforeApproval = live.events.length;
  const followerBeforeApproval = follower.events.length;
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
  const interactionId = String(pending.interaction_id);
  const nativeId = String(pending.tool_call_id);
  expect([pending.interaction_id, pending.tool_call_id].every((value) => typeof value === 'string' && value.length > 0)).toBe(true);
  // The response the approval resumed is the one now waiting on the question.
  const responseId = (await rpc<Task>(request, 'GetTask', { id: task.id })).metadata.response_id;
  expect(responseId, 'a task waiting on a question names the response it pauses').not.toBe('');
  expect(sessionOutputRows(sessionId, responseId).some((row) => {
    const part = row.part as { type?: string; data?: Record<string, unknown> };
    return part.type === 'data-interaction' && part.data?.interaction_id === interactionId
      && part.data?.tool_call_id === nativeId;
  }), 'the response recorded the question the task shows').toBe(true);
  await expect.poll(() => nativeToolCalls(sessionId, 'AskUserQuestion').length,
    { timeout: Math.min(15_000, remaining()) }).toBe(1);
  const nativeQuestion = nativeToolCalls(sessionId, 'AskUserQuestion')[0];
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
  // Both subscribers saw the same task resume after the Write authorization and
  // end on the same final status. Each projects the task as it reads it, so
  // their intermediate snapshots need not coincide.
  for (const [connection, from] of [[live, beforeApproval], [follower, followerBeforeApproval]] as const) {
    const resumedAfter = connection.events.slice(from).filter((event) => event.statusUpdate?.status.state === 'TASK_STATE_WORKING');
    expect(resumedAfter.length, 'both streams must resume the same task after Write authorization').toBeGreaterThan(0);
    expect(resumedAfter.every((event) => event.statusUpdate?.taskId === task.id
      && event.statusUpdate?.contextId === task.contextId)).toBe(true);
  }
  const finalStatus = (events: StreamEvent[]) => events.filter((event) => event.statusUpdate).at(-1)?.statusUpdate;
  expect(finalStatus(follower.events), 'both streams end on the same final status').toEqual(finalStatus(live.events));
  const evidence = await finished(api, request, task, marker, remaining);
  expect(evidence.task.metadata.response_id).toBe(responseId);
  expect(await api.downloadFileText(sessionId, `/workspace/${filename}`, Math.min(15_000, remaining())))
    .toBe(marker);
  const interactions = sessionInteractionRows(sessionId);
  const interaction = interactions.find((row) => row.interaction_id === interactionId);
  expect(interaction).toMatchObject({ status: 'answered', answer, response_id: responseId });
  expect(interaction?.contract.tool_call_id).toBe(nativeQuestion.id);
  const approvalRecord = interactions.find((row) => row.interaction_id === approval.interaction_id);
  expect(approvalRecord, 'the approval paused the same response the question did')
    .toMatchObject({ status: 'answered', answer: { decision: 'approve' }, response_id: responseId });
  expect(approvalRecord?.contract.tool_call_id).toBe(nativeWrite.id);
  const canonicalResponses = evidence.messages.messages.filter((row) => row.role === 'assistant' && row.id === responseId);
  expect(canonicalResponses, 'this Task has one canonical response for its logical input').toHaveLength(1);
  const canonicalParts = canonicalResponses[0].parts;
  const finalArtifacts = [
    { surface: 'GetTask', parts: resultParts(evidence.task.artifacts?.flatMap((artifact) => artifact.parts) ?? []) },
    ...[live, follower].map((connection, index) => {
      const finals = connection.events.filter((event) => event.artifactUpdate?.lastChunk === true);
      expect(finals.length, `SSE ${index + 1} must finalize its result artifact`).toBeGreaterThan(0);
      return { surface: `SSE ${index + 1}`, parts: resultParts(finals.at(-1)!.artifactUpdate!.artifact.parts) };
    }),
  ];
  for (const { surface, parts } of [{ surface: 'Session', parts: canonicalParts }, ...finalArtifacts]) {
    // Required tool identities originate in native supplier custody, not in the A2A adapter.
    for (const tool of [nativeWrite, nativeQuestion]) {
      const shown = toolParts(parts, tool);
      expect(shown, `${surface}: native ${tool.name} call`).toHaveLength(1);
      expect(shown[0].state, `${surface}: matching ${tool.name} result`).toBe('output-available');
    }
    const questionResult = toolParts(parts, nativeQuestion)[0];
    expect(JSON.stringify(questionResult.output), `${surface}: preserve the committed questionnaire answer`).toContain('YES');
    expect(parts, `${surface}: complete canonical response parts`).toEqual(canonicalParts);
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
  const responseId = current.metadata.response_id;
  expect(responseId, 'a working task names the response that runs it').not.toBe('');
  const originalInputs = sessionInputRows(sessionId);
  expect(originalInputs).toEqual([expect.objectContaining({ client_message_id: task.id, content: prompt,
    source: 'a2a' })]);
  const sandbox = await requireSandboxHandle(api, String(before.sandbox_id));
  killSandbox(sandbox);
  await waitForSandboxStopped(sandbox, Math.min(30_000, remaining()));
  expect(sandboxRunning(sandbox)).toBe(false);
  await api.waitForSession(sessionId, (row) => row.last_response?.response_id === responseId
    && row.last_response?.outcome === 'failed' && !(row.current_responses?.length), remaining());
  const failedEvent = await live.until((event) => event.statusUpdate?.status.state === 'TASK_STATE_FAILED');
  await expect.poll(() => { live.check(); return live.ended; }, { timeout: Math.min(10_000, remaining()) }).toBe(true);
  const failed = await rpc<Task>(request, 'GetTask', { id: task.id });
  expect(failed).toMatchObject({ id: task.id, contextId: task.contextId,
    status: { state: 'TASK_STATE_FAILED', message: { taskId: task.id, contextId: task.contextId } } });
  expect(failedEvent.statusUpdate?.status.message).toMatchObject({ taskId: task.id, contextId: task.contextId });
  // The platform ended the response for its lost box and recorded why; the
  // task tells its client that same reason.
  const terminal = sessionResponseRows(sessionId).find((row) => row.response_id === responseId);
  expect(terminal).toMatchObject({ status: 'ended', outcome: 'failed', native_reason: 'sandbox_lost' });
  const errorPart = sessionOutputRows(sessionId, responseId).map((row) => row.part)
    .find((part) => part.type === 'error');
  const reason = String(errorPart?.errorText || '');
  expect(reason, 'the response must retain the actual compute failure reason').not.toBe('');
  expect(failed.status.message!.parts.map((part) => part.text || '').join('\n')).toContain(reason);
  const history = await api.getMessages(sessionId, 50);
  expect(history.messages.filter((row) => row.role === 'user').map(messageText)).toEqual([prompt]);
  expect(sessionInputRows(sessionId), 'the dead task must not be automatically replayed').toEqual(originalInputs);
  expect(agentTaskReferences(agentId).filter((row) => row.kind === 'task')).toHaveLength(1);
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
  const recoveringSession = sessionRow(sessionId);
  expect(recoveringSession?.state === 'CREATING' || recoveringSession?.runtime_unavailable === true,
    'the new durable input must return while its runtime is still unavailable or starting').toBe(true);
  const nextAdmission = sessionInputRows(sessionId).find((row) => row.client_message_id === nextTask.id);
  expect(nextAdmission, 'the new input waits for the replacement runtime').toMatchObject({
    input_id: nextTask.metadata.input_id, source: 'a2a', content: nextPrompt, state: 'pending',
  });
  await recovery.until((event) => event.statusUpdate?.status.state === 'TASK_STATE_COMPLETED');
  const recovered = await finished(api, request, nextTask, nextMarker, remaining);
  const rebound = await api.getSession(sessionId);
  expect(rebound.sandbox_id).toBeTruthy();
  expect(rebound.sandbox_id).not.toBe(sandbox.sandboxId);
  expect(await rpc<Task>(request, 'GetTask', { id: task.id })).toMatchObject({ id: task.id,
    status: { state: 'TASK_STATE_FAILED', message: { taskId: task.id, contextId: task.contextId } } });
  const inputsAfterRecovery = sessionInputRows(sessionId);
  expect(inputsAfterRecovery).toHaveLength(2);
  expect(inputsAfterRecovery[0]).toEqual(originalInputs[0]);
  expect(inputsAfterRecovery[1]).toMatchObject({ client_message_id: nextTask.id, content: nextPrompt });
  expect(nativeToolCalls(sessionId, 'Bash'), 'restoring native history must not re-execute the original held tool')
    .toEqual([originalBash]);
  expect(recovered.messages.messages.filter((row) => row.role === 'user').map(messageText)).toEqual([prompt, nextPrompt]);
  const taskReferences = recovered.references.filter((row) => row.kind === 'task');
  expect(taskReferences).toHaveLength(2);
  expect(taskReferences.find((row) => row.reference_id === nextTask.id)).toMatchObject({ message_id: nextInput.message.messageId });
  expect(taskReferences.find((row) => row.reference_id === task.id)?.message_id).not.toBe(nextInput.message.messageId);
  await info.attach('a2a-compute-loss', { contentType: 'application/json',
    body: JSON.stringify({ task, failed, sandbox, terminal, errorPart, history, originalInputs, originalBash,
      events: live.events, nextTask, nextAdmission, recoveringSession,
      recovered, recoveryEvents: recovery.events }) });
});
