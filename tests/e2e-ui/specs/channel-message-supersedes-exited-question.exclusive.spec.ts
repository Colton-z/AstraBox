/**
 * A channel thread keeps working after its question's runner or whole box dies.
 *
 * The sibling spec covers the live case: a newer message stops the response
 * waiting on the open AskUserQuestion through the engine, which records an
 * error result for that tool, and is then answered by a response of its own.
 * Here the original engine wait is gone before the newer message arrives,
 * either with its box or with its runner.
 *
 * That difference is load-bearing for the ingress spine, which stops a
 * question still open in the conversation before it sends a newer message. If
 * stopping a wait that is not there failed the message, every later message in
 * the thread would meet the same closed door — a channel participant has no
 * browser composer and no card to dismiss, so nothing they can do clears it.
 * The Session's reader learns of the loss on its own (a lost box ends the
 * response that waited; a replaced runner does not report it), so the
 * question may already be closed when the newer message is driven, or still
 * open; the thread must keep working either way.
 *
 * The newer input runs on replacement compute after whole-box death, or on
 * the original box after runner death. Neither path may store an answer for
 * the question whose callback process has exited, or write one into native
 * SessionStore. Claude's resume can close that unresolved tool with its own
 * interrupted result; that is not a user answer or a platform-authored tool
 * result.
 */
import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi, messageText, messageToolCalls } from '../fixtures/astraApi';
import { channelCallback } from '../fixtures/channelCallback';
import {
  nativeTranscriptRows,
  postgresRows,
  sessionInputRows,
  sessionInteractionRows,
  sessionResponseRows,
  sqlLiteral,
} from '../fixtures/dbOracle';
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

interface NativeRow {
  seq: number;
  /** The runtime's own conversation id the entry is filed under. */
  session_id: string;
  entry: Record<string, unknown>;
}

const sessions = trackSessions();
let agentId = '';
let deploymentId = '';
let activeSessionId = '';
const evidence: Record<string, unknown> = {};
let recipient: Awaited<ReturnType<typeof channelCallback>> | null = null;

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('expected an evidence object');
  return value as Record<string, unknown>;
}

/**
 * Claude's main transcript as the platform holds it, in the runtime's own
 * order: the vendor's record, independent of what the platform projected
 * from it.
 */
function nativeRows(sessionId: string): NativeRow[] {
  return nativeTranscriptRows(sessionId)
    .filter((row) => row.subpath === null)
    .map((row) => ({
      seq: Number(row.seq),
      session_id: row.native_session_id,
      entry: object(JSON.parse(row.entry_json)),
    }));
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

/** Read evidence for an attachment without letting a broken read replace the test's own failure. */
function observe(read: () => unknown): unknown {
  try { return read(); } catch (error) { return { unavailable: String(error) }; }
}

test.beforeEach(() => {
  agentId = ''; deploymentId = ''; activeSessionId = '';
  for (const key of Object.keys(evidence)) delete evidence[key];
});

test.afterEach(async ({ request }, info) => {
  if (recipient) {
    evidence.channel = { deliveries: recipient.deliveries, errors: recipient.errors };
    await recipient.close();
    recipient = null;
  }
  const failed = ['failed', 'timedOut', 'interrupted'].includes(String(info.status));
  await info.attach('superseded-exited-question-evidence', {
    body: JSON.stringify({ agentId, deploymentId, sessionId: activeSessionId, ...evidence,
      native: failed && activeSessionId ? observe(() => nativeRows(activeSessionId)) : [],
      inputs: failed && activeSessionId ? observe(() => sessionInputRows(activeSessionId)) : [],
      responses: failed && activeSessionId ? observe(() => sessionResponseRows(activeSessionId)) : [],
      interactions: failed && activeSessionId ? observe(() => sessionInteractionRows(activeSessionId)) : [],
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
      reply: { callback_url: recipient!.url },
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
      prepared_capacity: 0,
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
  recipient = await channelCallback();

  // No warm message. The whole-box fault puts the superseding message on a
  // cold re-borrow, and three live model replies plus that borrow do not fit
  // the suite's fixed 180s budget — measured, not guessed. The question
  // message creates the session by itself, so a warm message bought nothing
  // this spec needs.
  const questionMarker = `E2E_CHANNEL_EXITED_ASK_${runId}`;
  const questionText = [
    `${questionMarker}: call AskUserQuestion exactly once as your first and only tool.`,
    'Ask one single-select question with options YES and NO.',
    'Wait for the answer and do not answer the question yourself.',
  ].join(' ');
  const question = await triggerThreadMessage(request, {
    secret,
    threadId,
    messageId: `question-${runId}`,
    text: questionText,
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
  const parkedResponseId = String(pending.response_id || '').trim();
  expect(parkedResponseId, 'the open channel question must name the response it pauses').not.toEqual('');
  const waiting = await api.getSession(sessionId);
  const deadSandboxId = String(waiting.sandbox_id || '').trim();
  expect(deadSandboxId, 'the open channel question must still own its sandbox')
    .not.toEqual('');
  const interactionId = String(pending.interaction_id || '').trim();
  expect(interactionId).not.toBe('');
  const channelQuestion = () => postgresRows<Record<string, unknown>>(
    `SELECT state, answering_item FROM channel_interactions WHERE deployment_id = ${sqlLiteral(deploymentId)} `
      + `AND session_id = ${sqlLiteral(sessionId)} AND interaction_id = ${sqlLiteral(interactionId)}`,
  );
  await expect.poll(channelQuestion, { message: 'the original channel question was actually shown' })
    .toEqual([expect.objectContaining({ state: 'OPEN', answering_item: null })]);
  expect(recipient.errors).toEqual([]);
  expect(recipient.deliveries.some((delivery) => delivery.question?.interaction_id === interactionId)).toBe(true);
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
  const initialInteractions = sessionInteractionRows(sessionId);
  expect(initialInteractions.filter((row) => row.interaction_id === interactionId))
    .toEqual([expect.objectContaining({
      status: 'open', response_id: parkedResponseId, answer: null,
      contract: expect.objectContaining({ tool_call_id: toolCallId }),
    })]);
  // The message is the conversation's one input: the channel sent it once.
  const initialInputs = sessionInputRows(sessionId);
  expect(initialInputs.map((row) => ({ source: row.source, content: row.content })))
    .toEqual([{ source: 'channel', content: questionText }]);
  evidence.before = { fault, originalQuestion, pending, waiting, runtimeBefore, initialInputs, initialInteractions };

  // Out-of-band death, not a reclaim: a reclaim releases the box through the
  // platform, which is precisely the path this spec must NOT start from. The
  // engine's wait behind the question is gone without the platform being told.
  const originalHandle = await requireSandboxHandle(api, deadSandboxId);
  if (fault === 'sandbox-loss') {
    killSandbox(originalHandle);
  } else {
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
  // Drop the host's connection to the old runtime as well as the callback
  // process, so nothing in this process keeps answering for the dead wait.
  await api.adminEvictRuntime(sessionId);
  await expect.poll(channelQuestion, {
    message: 'runtime withdrawal closes the shown channel question without another inbound',
    timeout: 30_000,
  }).toEqual([expect.objectContaining({ state: 'CLOSED', answering_item: null })]);
  // Whether the platform still shows the question here is a race with its own
  // detection of the loss, not part of the contract; it is kept as evidence.
  evidence.afterFault = {
    session: await api.getSession(sessionId),
    interactions: sessionInteractionRows(sessionId),
  };

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

  // The thread's next message becomes one input of the same conversation, and
  // a response of its own answers it to completion.
  await expect.poll(() => sessionInputRows(sessionId).filter((row) => row.content === nextPrompt).length, {
    timeout: 120_000,
    message: 'the newer channel message must reach the conversation once, past the dead question',
  }).toBe(1);
  const nextInput = sessionInputRows(sessionId).find((row) => row.content === nextPrompt)!;
  expect(nextInput.source).toBe('channel');
  const nextReply = await api.waitForReplyTo(sessionId, nextInput.input_id);
  expect(nextReply.metadata?.outcome, 'the newer channel message must be answered to completion').toBe('completed');
  expect(nextReply.id, 'the newer message is answered by a response of its own').not.toBe(parkedResponseId);
  if (fault === 'sandbox-loss') {
    expect(messageText(nextReply)).toContain(`${nextMarker}_DONE`);
  } else {
    expect(messageText(nextReply).trim().length).toBeGreaterThan(0);
  }
  const completed = await api.waitForSessionReady(sessionId);
  expect(completed.last_response?.response_id, 'nothing ran after the newer message').toBe(nextReply.id);
  expect(completed.pending_interaction ?? null).toBeNull();
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
  const assistants = history.messages.filter((message) => message.role === 'assistant');
  expect(assistants, 'the newer message adds exactly one response').toHaveLength(assistantsBeforeNext + 1);
  expect(
    assistants.filter((message) => (fault === 'sandbox-loss'
      ? messageText(message).includes(`${nextMarker}_DONE`)
      : message.id === nextReply.id && messageText(message).trim().length > 0)),
    'the newer channel question must produce exactly one durable answer',
  ).toHaveLength(1);
  const parked = assistants.find((message) => messageToolCalls(message)
    .some((call) => call.toolCallId === toolCallId && call.toolName === 'AskUserQuestion'));
  expect(
    parked,
    'the abandoned question stays in history — it happened, it just went unanswered',
  ).toBeDefined();
  expect(parked?.id, 'the question belongs to the response that waited on it').toBe(parkedResponseId);
  expect(parked?.metadata?.outcome, 'the response that waited on the question ended with its runtime')
    .toBe('failed');
  expect(parked?.metadata?.native_reason).toBe(fault === 'sandbox-loss' ? 'sandbox_lost' : 'runtime_lost');

  const finalNative = nativeRows(sessionId);
  expect(finalNative.filter((row) => row.seq === originalQuestion.seq)).toEqual([originalQuestion]);
  const interruptedTool = finalNative.filter((row) => nativeBlocks(row.entry)
    .some((block) => block.type === 'tool_result' && block.tool_use_id === toolCallId));
  expect(interruptedTool,
    'Claude resume must close the unresolved tool exactly once, without inventing an answer').toHaveLength(1);
  // Claude Code 2.1.287's own closure for a tool whose session ended before
  // its result was recorded; it is still an interruption, never an answer.
  const interruption = "[Tool call interrupted: the session ended before this call's result was "
    + 'recorded, so its outcome is unknown. Check whether it took effect before relying on it or '
    + 'running it again.]';
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
  // No answer was ever stored for the dead question: the question closed with
  // its runtime, unanswered, and the conversation took exactly two messages.
  const finalInteraction = sessionInteractionRows(sessionId).filter((row) => row.interaction_id === interactionId);
  expect(finalInteraction, 'an abandoned question must not be turned into an answer')
    .toEqual([expect.objectContaining({ answer: null, answered_by: null, answered_at: null })]);
  expect(['withdrawn', 'expired'], 'the question must close with its runtime')
    .toContain(finalInteraction[0].status);
  expect(channelQuestion()).toEqual([expect.objectContaining({ state: 'CLOSED', answering_item: null })]);
  const finalInputs = sessionInputRows(sessionId);
  expect(finalInputs).toHaveLength(2);
  expect(finalInputs[0]).toEqual(initialInputs[0]);
  expect(finalInputs[1].input_id).toBe(nextInput.input_id);
  const nextContent = finalInputs[1].content;
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
  await expect.poll(() => recipient!.deliveries
    .filter((delivery) => delivery.question === null).map((delivery) => delivery.text.trim()), {
    message: 'the participant receives the native new reply once after the old question closes',
  }).toEqual([nativeReplies.map((row) => nativeText(row.entry)).join('\n\n').trim()]);
  expect(recipient!.errors).toEqual([]);
  evidence.completed = { completed, finalInputs, finalInteraction, finalNative, newInputs, nativeReplies, history };
  test.info().annotations.push({
    type: 'retired_tool_call_parts',
    description: JSON.stringify(
      history.messages
        .flatMap((message) => messageToolCalls(message))
        .filter((call) => call.toolCallId === toolCallId),
    ),
  });
}
