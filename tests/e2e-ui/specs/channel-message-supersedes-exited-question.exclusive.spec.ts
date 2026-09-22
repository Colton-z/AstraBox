/**
 * A channel thread keeps working after its question's runner or whole box dies.
 *
 * The sibling spec covers the live case: a newer message declines the open
 * AskUserQuestion through the engine, which records an error result for that
 * tool, and then runs its own turn. Here the original engine wait is gone
 * before the newer message arrives, either with its box or with its runner.
 *
 * That difference is load-bearing for the ingress spine, which declines the
 * pending question as a precondition of dispatching. If declining a wait that
 * is not there fails the dispatch, the 409 takes the whole message with it and
 * every later message in the thread meets the same closed door — a channel
 * participant has no browser composer and no card to dismiss, so nothing they
 * can do clears it.
 *
 * The newer input runs on replacement compute after whole-box death, or on
 * the original box after runner death. Neither path may insert an answer in
 * native SessionStore for the question whose callback process has exited.
 * Claude's resume can close that unresolved tool with its own interrupted
 * result; that is not a user answer or a platform-authored tool result.
 */
import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { documentsByField, sessionEvents } from '../fixtures/dbOracle';
import { PlatformApi } from '../fixtures/platformApi';
import { IMAGE_RUNNER_LAUNCHER, imageRunnerPidLookup, imageRunnerRestartScript } from '../fixtures/runnerRestart';
import { killSandbox, requireSandboxHandle, sandboxExec } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { apiPath } from '../fixtures/env';

interface ChannelTriggerReceipt {
  deployment_id: string;
  session_id: string;
  status: string;
  [key: string]: unknown;
}

const sessions = trackSessions();
let agentId = '';
let deploymentId = '';
let activeSessionId = '';
const evidence: Record<string, unknown> = {};

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('expected an evidence object');
  return value as Record<string, unknown>;
}

function nativeRows(sessionId: string) {
  return documentsByField('transcript_entries', '$.platform_session_id', sessionId)
    .filter((row) => row.subpath == null)
    .map((row) => ({ seq: Number(row.seq), session_id: row.session_id,
      entry: object(JSON.parse(String(row.entry_json))) }))
    .sort((left, right) => left.seq - right.seq);
}

function nativeBlocks(entry: Record<string, unknown>): Record<string, unknown>[] {
  if (!entry.message || typeof entry.message !== 'object') return [];
  const content = object(entry.message).content;
  return Array.isArray(content) ? content.map(object) : [];
}

function nativeText(entry: Record<string, unknown>): string {
  if (!entry.message || typeof entry.message !== 'object') return '';
  const content = object(entry.message).content;
  return typeof content === 'string' ? content : nativeBlocks(entry)
    .filter((block) => block.type === 'text').map((block) => String(block.text || '')).join('');
}

function originalToolRows(sessionId: string, toolCallId: string) {
  return nativeRows(sessionId).filter((row) => nativeBlocks(row.entry)
    .some((block) => block.type === 'tool_use' && block.id === toolCallId));
}

test.beforeEach(() => {
  agentId = ''; deploymentId = ''; activeSessionId = '';
  for (const key of Object.keys(evidence)) delete evidence[key];
});

test.afterEach(async ({ request }, info) => {
  const failed = ['failed', 'timedOut', 'interrupted'].includes(String(info.status));
  await info.attach('superseded-exited-question-evidence', {
    body: JSON.stringify({ agentId, deploymentId, sessionId: activeSessionId, ...evidence,
      native: failed && activeSessionId ? nativeRows(activeSessionId) : [],
      journal: failed && activeSessionId ? sessionEvents(activeSessionId) : [],
      interactions: failed && activeSessionId
        ? documentsByField('interaction_snapshots', '$.session_id', activeSessionId) : [],
      history: failed && activeSessionId ? await new AstraApi(request).getMessages(activeSessionId, 100) : null }),
    contentType: 'application/json',
  });
});

onPassOnly(async ({ request }) => {
  if (deploymentId && agentId) {
    await new PlatformApi(request).deleteDeployment(agentId, deploymentId);
  }
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
});

async function triggerThreadMessage(
  request: APIRequestContext,
  {
    secret,
    threadId,
    messageId,
    text,
  }: {
    secret: string;
    threadId: string;
    messageId: string;
    text: string;
  },
): Promise<ChannelTriggerReceipt> {
  const response = await request.fetch(apiPath(`/deployments/${deploymentId}/trigger`), {
    method: 'POST',
    data: {
      text,
      message_id: messageId,
      conversation_id: threadId,
    },
    headers: { 'x-channel-secret': secret },
    timeout: 120_000,
  });
  const raw = await response.text();
  expect(response.ok(), raw).toBe(true);
  const envelope = JSON.parse(raw) as {
    data?: ChannelTriggerReceipt;
  } & ChannelTriggerReceipt;
  return (envelope.data ?? envelope) as ChannelTriggerReceipt;
}

test('a newer channel message runs after the question it supersedes lost its runtime', async ({
  request,
}) => {
  await runSupersededQuestion(request, 'sandbox-loss');
});

test('a newer channel message retires an exited question without a native tool result on the same sandbox being fabricated by the platform', async ({ request }) => {
  await runSupersededQuestion(request, 'runner-loss');
});

async function runSupersededQuestion(request: APIRequestContext, fault: 'sandbox-loss' | 'runner-loss') {
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const runId = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  const threadId = `e2e-channel-exited-ask-${runId}`;
  const base = await api.defaultAgent();
  const environmentName = String(base.environment_name || '').trim();
  expect(environmentName, 'the seeded Agent must name an environment').not.toEqual('');
  let model = String(base.model || '').trim();
  if (!model || model.includes('*')) {
    const models = await api.listEnvironmentModels(environmentName);
    model = models.find((candidate) => candidate && !candidate.includes('*')) || '';
  }
  expect(model, 'the Agent environment must expose a concrete model').not.toEqual('');

  const agent = fault === 'runner-loss'
    ? await api.createColdTestAgent(`__e2e_channel_exited_runner_${runId}`)
    : await api.createAgent({
      name: `__e2e_channel_exited_ask_${runId}`,
      model,
      environment_name: environmentName,
    });
  agentId = String(agent.agent_id || '').trim();
  const deployment = await platform.createDeployment(agentId, {
    scene: 'channel:generic_json',
    prompt_prefix: '',
  });
  deploymentId = String(deployment.deployment_id || '').trim();
  const secret = String(deployment.secret || '').trim();
  expect(deploymentId, 'the channel deployment must expose its id').not.toEqual('');
  expect(secret, 'the channel deployment must expose its trigger secret').not.toEqual('');

  // No warm turn. The whole-box fault puts the superseding message on a
  // cold re-borrow, and three live model turns plus that borrow do not fit the
  // suite's fixed 180s budget — measured, not guessed. The question trigger
  // creates the session by itself, so the warm turn bought nothing this spec
  // needs.
  const questionMarker = `E2E_CHANNEL_EXITED_ASK_${runId}`;
  const question = await triggerThreadMessage(request, {
    secret,
    threadId,
    messageId: `question-${runId}`,
    text: [
      `${questionMarker}: call AskUserQuestion exactly once as your first and only tool.`,
      'Ask one single-select question with options YES and NO.',
      'Wait for the answer and do not answer the question yourself.',
    ].join(' '),
  });
  expect(question.status).toBe('accepted');
  const sessionId = String(question.session_id || '').trim();
  expect(sessionId, 'the channel trigger must name the conversation it created')
    .not.toEqual('');
  sessions.push(sessionId);
  activeSessionId = sessionId;

  const pending = await api.waitForPendingInteraction(sessionId);
  expect(String(pending.tool_name || '')).toBe('AskUserQuestion');
  const toolCallId = String(pending.tool_call_id || '').trim();
  expect(toolCallId, 'the open channel question must expose its engine tool id')
    .not.toEqual('');
  const waiting = await api.getSession(sessionId);
  const deadSandboxId = String(waiting.sandbox_id || '').trim();
  expect(deadSandboxId, 'the open channel question must still own its sandbox')
    .not.toEqual('');
  const interactionId = String(pending.interaction_id || '').trim();
  expect(interactionId).not.toBe('');
  await expect.poll(() => originalToolRows(sessionId, toolCallId).length,
    { message: 'the real unanswered native tool call must be durable before the fault' }).toBe(1);
  const originalQuestion = originalToolRows(sessionId, toolCallId)[0];
  expect(String(originalQuestion.session_id || '')).not.toBe('');
  const runtimeBefore = await api.adminSessionDetail(sessionId);
  expect(originalQuestion.session_id).toBe(runtimeBefore.engine_session_key);
  expect(nativeBlocks(originalQuestion.entry).find((block) => block.id === toolCallId)?.name)
    .toBe('AskUserQuestion');
  expect(nativeRows(sessionId).flatMap((row) => nativeBlocks(row.entry))
    .filter((block) => block.type === 'tool_result' && block.tool_use_id === toolCallId)).toEqual([]);
  const initialInteractions = documentsByField('interaction_snapshots', '$.session_id', sessionId);
  expect(initialInteractions.filter((row) => row.interaction_id === interactionId))
    .toEqual([expect.objectContaining({ interaction_state: 'OPEN', active: true, tool_call_id: toolCallId })]);
  const initialCommands = sessionEvents(sessionId).filter((row) => row.event_type === 'command.accepted'
    && object(row.payload).command_type === 'StartTurn');
  expect(initialCommands).toHaveLength(1);
  expect(object(initialCommands[0].payload).command_type).toBe('StartTurn');
  evidence.before = { fault, originalQuestion, pending, waiting, runtimeBefore, initialCommands, initialInteractions };

  // Out-of-band death, not a reclaim: a reclaim settles the parked turn on its
  // way out, which is precisely the state this spec must NOT start from. The
  // platform still reports a pending interaction; the wait behind it is gone.
  const originalHandle = await requireSandboxHandle(api, deadSandboxId);
  if (fault === 'sandbox-loss') {
    killSandbox(originalHandle);
  } else {
    expect(runtimeBefore.has_local_runtime).toBe(true);
    expect(runtimeBefore.runtime_identity?.sandbox_id).toBe(deadSandboxId);
    expect(String(runtimeBefore.runtime_identity?.isolated_session_id || '')).toBe('');
    const oldPid = Number(sandboxExec(originalHandle,
      `${imageRunnerPidLookup()}\nprintf '%s\\n' "$1"`).trim());
    expect(Number.isInteger(oldPid) && oldPid > 1).toBe(true);
    const restart = sandboxExec(originalHandle, imageRunnerRestartScript({
      signal: 'KILL',
      launch: IMAGE_RUNNER_LAUNCHER,
      log: '/tmp/astrabox-superseded-question-runner.log',
      evidence: 'printf "old_pid=%s\\n" "$old_pid"',
      name: 'superseded-question image runner',
    }));
    expect(restart.trim()).toBe(`old_pid=${oldPid}`);
    const newPid = Number(sandboxExec(originalHandle,
      `${imageRunnerPidLookup()}\nprintf '%s\\n' "$1"`).trim());
    expect(Number.isInteger(newPid) && newPid > 1).toBe(true);
    expect(newPid).not.toBe(oldPid);
    expect(await requireSandboxHandle(api, deadSandboxId)).toEqual(originalHandle);
    evidence.runnerRestart = { oldPid, newPid, restart, originalHandle };
  }
  // Evict the host client as well as terminating the callback process. A cached
  // client could otherwise decline the question without exercising recovery.
  await api.adminEvictRuntime(sessionId);
  const stillPending = await api.getSession(sessionId);
  expect(
    stillPending.pending_interaction,
    'the fixture requires a question the platform still believes is answerable',
  ).not.toBeNull();
  expect(stillPending.pending_interaction?.interaction_id).toBe(interactionId);
  expect(documentsByField('interaction_snapshots', '$.session_id', sessionId)
    .filter((row) => row.interaction_id === interactionId))
    .toEqual([expect.objectContaining({ interaction_state: 'OPEN', active: true, tool_call_id: toolCallId })]);
  expect(nativeRows(sessionId).filter((row) => row.seq === originalQuestion.seq)).toEqual([originalQuestion]);
  expect(nativeRows(sessionId).flatMap((row) => nativeBlocks(row.entry))
    .filter((block) => block.type === 'tool_result' && block.tool_use_id === toolCallId)).toEqual([]);

  const nextMarker = `E2E_CHANNEL_AFTER_EXIT_${runId}`;
  const assistantsBeforeNext = await api.assistantCount(sessionId);
  const nextPrompt = fault === 'sandbox-loss'
    ? `${nextMarker}: this is a new question. Reply with exactly ${nextMarker}_DONE and do not use tools.`
    : `${nextMarker}: this is a new independent question. In one short sentence explain what a notebook is. Do not use tools.`;
  const next = await triggerThreadMessage(request, {
    secret,
    threadId,
    messageId: `next-${runId}`,
    text: nextPrompt,
  });
  expect(next.status).toBe('accepted');
  expect(next.session_id, 'the same external thread must keep one conversation')
    .toBe(sessionId);

  await api.waitForAssistantMessageMatching(
    sessionId,
    assistantsBeforeNext,
    (message) => fault === 'sandbox-loss'
      ? messageText(message).includes(`${nextMarker}_DONE`)
      : messageText(message).trim().length > 0,
  );
  const completed = await api.waitForSession(sessionId, (session) => (
    session.state === 'READY'
    && !session.current_turn_id
    && !session.pending_interaction
    && session.last_turn_status === 'COMPLETED'
  ));
  if (fault === 'sandbox-loss') {
    expect(
      String(completed.sandbox_id || ''),
      'the superseding message must run on compute that is actually alive',
    ).not.toBe(deadSandboxId);
  } else {
    expect(completed.sandbox_id).toBe(deadSandboxId);
    expect(await requireSandboxHandle(api, deadSandboxId)).toEqual(originalHandle);
  }
  expect(String(completed.sandbox_id || ''), 'and it must have borrowed one')
    .not.toEqual('');

  const history = await api.getMessages(sessionId, 100);
  expect(
    history.messages.filter((message) => (
      message.role === 'assistant' && (fault === 'sandbox-loss'
        ? messageText(message).includes(`${nextMarker}_DONE`)
        : message.turn_id === completed.last_turn_id && messageText(message).trim().length > 0)
    )),
    'the newer channel question must produce exactly one durable answer',
  ).toHaveLength(1);
  const parked = history.messages.find((message) => (
    (message.blocks || []).some((block) => (
      block.type === 'tool_use' && block.id === toolCallId
    ))
  ));
  expect(
    parked,
    'the abandoned question stays in history — it happened, it just went unanswered',
  ).toBeDefined();
  const finalNative = nativeRows(sessionId);
  expect(finalNative.filter((row) => row.seq === originalQuestion.seq)).toEqual([originalQuestion]);
  const interruptedTool = finalNative.filter((row) => nativeBlocks(row.entry)
    .some((block) => block.type === 'tool_result' && block.tool_use_id === toolCallId));
  expect(interruptedTool,
    'Claude resume must close the unresolved tool exactly once, without inventing an answer').toHaveLength(1);
  const interruption = '[Request interrupted by user for tool use]';
  expect(nativeBlocks(interruptedTool[0].entry)).toEqual([{
    type: 'tool_result', tool_use_id: toolCallId, is_error: true, content: interruption,
  }]);
  expect(interruptedTool[0].entry).toMatchObject({
    type: 'user',
    toolUseResult: interruption,
    toolDenialKind: 'interrupted',
    sourceToolAssistantUUID: originalQuestion.entry.uuid,
    parentUuid: originalQuestion.entry.uuid,
  });
  const finalAccepted = sessionEvents(sessionId).filter((row) => row.event_type === 'command.accepted');
  const finalCommands = finalAccepted.filter((row) => object(row.payload).command_type === 'StartTurn');
  expect(finalAccepted.filter((row) => object(row.payload).command_type === 'AnswerInteraction'),
    'an abandoned question must not be turned into an accepted answer command').toEqual([]);
  expect(finalCommands).toHaveLength(2);
  expect(finalCommands[0]).toEqual(initialCommands[0]);
  expect(object(finalCommands[1].payload).command_type).toBe('StartTurn');
  expect(finalCommands[1].turn_id).not.toBe(initialCommands[0].turn_id);
  const nextContent = String(object(finalCommands[1].payload).content);
  expect(nextContent).toBe(nextPrompt);
  const newInputs = finalNative.filter((row) => row.entry.type === 'user' && nativeText(row.entry) === nextContent);
  expect(newInputs).toHaveLength(1);
  expect(newInputs[0].entry.uuid).toEqual(expect.any(String));
  expect(interruptedTool[0].seq,
    'native resume closes the abandoned tool before the independent new input').toBeLessThan(newInputs[0].seq);
  const descendants = new Set([String(newInputs[0].entry.uuid)]);
  const nativeReplies = finalNative.filter((row) => {
    if (!descendants.has(String(row.entry.parentUuid || ''))) return false;
    if (row.entry.type === 'user' && nativeText(row.entry)) return false;
    if (typeof row.entry.uuid === 'string') descendants.add(row.entry.uuid);
    return row.entry.type === 'assistant' && row.entry.isSidechain !== true && nativeText(row.entry).trim();
  });
  expect(nativeReplies.length).toBeGreaterThan(0);
  expect(finalNative.every((row) => row.session_id === originalQuestion.session_id)).toBe(true);
  expect(documentsByField('interaction_snapshots', '$.session_id', sessionId)
    .filter((row) => row.interaction_id === interactionId))
    .toEqual([expect.objectContaining({ active: false, tool_call_id: toolCallId })]);
  evidence.completed = { completed, finalCommands, finalNative, newInputs, nativeReplies, history };
  test.info().annotations.push({
    type: 'retired_tool_call_blocks',
    description: JSON.stringify(
      history.messages
        .flatMap((message) => message.blocks || [])
        .filter((block) => (
          (block.type === 'tool_result' && block.tool_use_id === toolCallId)
          || (block.type === 'tool_use' && block.id === toolCallId)
        )),
    ),
  });
}
